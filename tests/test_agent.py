import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from backend.agent import Agent
from backend.llm import ChatClient, ModelConfig, ModelError, message_to_dict, parse_response
from backend.memory_store import JsonMemoryStore
from backend.models import Message, Role, ToolCall
from backend.tools import ToolRegistry, register_file_tools


def response(content=None, calls=None, finish="stop"):
    return {"choices": [{
        "finish_reason": finish,
        "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
    }]}


def wire_call(code, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {
        "name": "run_code", "arguments": json.dumps({"code": code}),
    }}


class ScriptedClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages):# 完全模拟地返回预设的回复
        self.requests.append(list(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = ToolRegistry()
        register_file_tools(self.registry, self.root)
        self.store = JsonMemoryStore(self.root / "memory")

    def test_api_execution_feedback_and_final_answer(self):
        # 临时样例，不触碰项目内的 error.py。
        (self.root / "sample.py").write_text("value = 1\n", encoding="utf-8")
        packets = [
            response(calls=[wire_call('print(read_file("sample.py"))')], finish="tool_calls"),
            response(calls=[wire_call(
                'write_file("sample.py", "value = 2\\n", overwrite=True)\n'
                'print(read_file("sample.py"))', "call_2",
            )], finish="tool_calls"),
            response("原值为 1，已改成 2，并重新读取确认。"),
        ]
        sent = []

        def transport(request, timeout):
            sent.append(json.loads(request.data))
            return io.BytesIO(json.dumps(packets[len(sent) - 1]).encode())

        client = ChatClient(ModelConfig("https://model.example/custom/chat", "custom-model"))
        agent = Agent(client, self.registry, lambda code: True, store=self.store)
        with patch("backend.llm.urlopen", side_effect=transport):
            result = agent.run("将 sample.py 的 value 改为 2，并说明原值")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.rounds, 3)
        self.assertEqual((self.root / "sample.py").read_text(), "value = 2\n")
        feedback = sent[1]["messages"][-1]
        self.assertEqual(feedback["role"], "tool")
        self.assertEqual(feedback["tool_call_id"], "call_1")
        self.assertIn("value = 1", json.loads(feedback["content"])["output"])
        self.assertIn("value = 2", sent[2]["messages"][-1]["content"])
        for parent, child in zip(agent.history, agent.history[1:]):
            self.assertEqual(child.parent_id, parent.id)
            self.assertEqual(parent.children_ids, [child.id])

    def test_execution_error_is_returned_to_model(self):
        client = ScriptedClient([
            Message(Role.ASSISTANT, tool_calls=[ToolCall("run_code", {"code": "1 / 0"}, "c1")]),
            Message(Role.ASSISTANT, "执行失败，尚未修改文件。"),
        ])
        agent = Agent(client, self.registry, lambda code: True, store=self.store)
        self.assertEqual(agent.run("检查文件").status, "completed")
        feedback = json.loads(client.requests[1][-1].content)
        self.assertEqual(feedback["status"], "error")
        self.assertIn("ZeroDivisionError", feedback["error"])

    def test_denial_cancels_remaining_calls_without_execution(self):
        calls = [ToolCall("run_code", {"code": 'write_file("new.txt", "x")'}, f"c{i}") for i in range(2)]
        client = ScriptedClient([Message(Role.ASSISTANT, tool_calls=calls)])
        approvals = []
        agent = Agent(client, self.registry, lambda code: approvals.append(code) or False, store=self.store)
        self.assertEqual(agent.run("测试拒绝").status, "cancelled")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(len(approvals), 1)
        self.assertEqual([entry.message.tool_call_id for entry in agent.history[-2:]], ["c0", "c1"])

    def test_invalid_arguments_and_round_limit(self):
        client = ScriptedClient([Message(Role.ASSISTANT, tool_calls=[
            ToolCall("run_code", {"code": "print(1)", "approved": True}),
        ])])
        agent = Agent(client, self.registry, lambda code: self.fail("不能让模型批准自己"), max_rounds=1, store=self.store)
        self.assertEqual(agent.run("检查").status, "limit")
        self.assertEqual(json.loads(agent.history[-1].message.content)["status"], "error")

    def test_api_error_is_not_retried(self):
        client = ScriptedClient([ModelError("请求失败")])
        result = Agent(client, self.registry, lambda code: True, store=self.store).run("检查")
        self.assertEqual(result.status, "error")
        self.assertEqual(len(client.requests), 1)


class ClientTests(unittest.TestCase):
    def test_base_url_normalization_preserves_custom_endpoints(self):
        for endpoint in (
            "https://model.example", "https://model.example/",
            "https://model.example/v1", "https://model.example/v1/",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(ModelConfig(endpoint, "m").endpoint,
                                 "https://model.example/v1/chat/completions")
        custom = "https://model.example/custom/chat?version=1"
        self.assertEqual(ModelConfig(custom, "m").endpoint, custom)

    def test_invalid_responses_are_distinguished_without_body_leaks(self):
        config = ModelConfig("https://model.example/v1", "m")
        cases = [
            (b"", "空正文"),
            (b"<!DOCTYPE html><html>private</html>", "HTML"),
            (b'data: {"private": true}\n\n', "SSE"),
            (b"private", "不是有效 JSON"),
        ]
        for body, hint in cases:
            with self.subTest(hint=hint):
                with patch("backend.llm.urlopen", return_value=io.BytesIO(body)):
                    with self.assertRaises(ModelError) as caught:
                        ChatClient(config).complete([Message(Role.USER, "检查")])
                self.assertIn(hint, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    def test_environment_and_custom_auth_headers(self):
        env = {
            "MODEL_API_URL": "https://model.example/custom/chat",
            "MODEL_NAME": "my-model", "MODEL_API_KEY": "test-secret",
            "MODEL_AUTH_HEADER": "api-key", "MODEL_AUTH_PREFIX": "",
            "MODEL_EXTRA_HEADERS": '{"X-Version": "1"}', "MODEL_TIMEOUT": "12",
        }
        with patch.dict("os.environ", env, clear=True):
            config = ModelConfig.from_env()
        with patch("backend.llm.urlopen", return_value=io.BytesIO(json.dumps(response("done")).encode())) as send:
            reply = ChatClient(config).complete([Message(Role.USER, "检查")])
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, env["MODEL_API_URL"])
        self.assertEqual(request.get_header("Api-key"), "test-secret")
        self.assertEqual(request.get_header("X-version"), "1")
        self.assertEqual(send.call_args.kwargs["timeout"], 12)
        body = json.loads(request.data)
        self.assertEqual(body["model"], "my-model")
        self.assertEqual(body["tools"][0]["function"]["name"], "run_code")
        self.assertNotIn("test-secret", repr(config))
        self.assertEqual(reply.content, "done")

    def test_response_validation_and_serialization(self):
        packet = response(calls=[wire_call("print(1)")], finish="tool_calls")
        reply = parse_response(packet)
        self.assertEqual(message_to_dict(reply)["tool_calls"], packet["choices"][0]["message"]["tool_calls"])
        invalid = [
            response("partial", finish="length"), response(), {"choices": []},
            response(calls=[wire_call("x"), wire_call("y")], finish="tool_calls"),
        ]
        for packet in invalid:
            with self.subTest(packet=packet), self.assertRaises(ModelError):
                parse_response(packet)

    def test_transport_errors_hide_credentials_and_body(self):
        config = ModelConfig("https://model.example/chat", "m", api_key="test-secret")
        errors = [
            HTTPError(config.endpoint, 401, "test-secret", {}, io.BytesIO(b"test-secret")),
            URLError("test-secret"), TimeoutError("test-secret"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch("backend.llm.urlopen", side_effect=error) as send:
                    with self.assertRaises(ModelError) as caught:
                        ChatClient(config).complete([Message(Role.USER, "检查")])
                self.assertNotIn("test-secret", str(caught.exception))
                self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
