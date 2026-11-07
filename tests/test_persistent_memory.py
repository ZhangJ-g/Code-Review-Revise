import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.agent import Agent
from backend.api.app import create_app
from backend.api.serializers import conversation_snapshot, history_revision
from backend.api.service import AgentService
from backend.compaction_manager import CompactionManager
from backend.context_manager import ContextConfig, ContextManager
from backend.persistent_memory_management import PersistentMemoryManager
from backend.memory_store import JsonMemoryStore, MemoryConflict, PersistentMemoryStore, history_to_dict
from backend.model_api import ErrorKind, ModelError, RetryPolicy
from backend.models import Message, Role, ToolCall
from backend.prompts import MEMORY_RULES
from backend.tools import ToolRegistry


def memory_messages(messages):
    result = []
    for message in messages:
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("context_type") == "long_term_memory":
            result.append(payload)
    return result


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages):
        self.requests.append(messages)
        reply = next(self.replies)
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return reply


class Summarizer:
    def __init__(self, callback=None):
        self.requests = []
        self.callback = callback

    def summarize(self, messages, *, max_output_tokens):
        self.requests.append(messages)
        if self.callback:
            self.callback()
        return "此前任务已结束，保留已有约定和验证记录。"


class WorkerClient:
    """真实进程/审批/runner 闭环，模型响应为替身，不访问外部 API。"""
    def __init__(self):
        self.round = 0

    def complete(self, messages):
        self.round += 1
        if self.round == 1:
            return Message(Role.ASSISTANT, tool_calls=[ToolCall("run_code", {"code": 'print("approved")'})])
        active = memory_messages(messages)
        return Message(Role.ASSISTANT, active[0]["content"] if active else "none")


def worker_factory(approve, *, store, root, observer):
    config = ContextConfig(100000, 1000)
    compactor = CompactionManager(Summarizer(), store, max_input_tokens=99000, max_summary_tokens=1000)
    return Agent(WorkerClient(), ToolRegistry(), approve, store=store,
                 context_manager=ContextManager(config, compactor, []))


class PersistentMemoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manager = PersistentMemoryManager(PersistentMemoryStore(self.root / "memory"))
        self.history_store = JsonMemoryStore(self.root / "memory")

    def create(self, name="项目约定", content="使用 Python", apply=False):
        result = self.manager.execute("create", name=name, content=content)
        memory_id = result["memory_id"]
        if apply:
            self.manager.execute("apply", memory_id=memory_id)
        return memory_id

    def context(self, summarizer=None, config=None):
        compactor = CompactionManager(summarizer or Summarizer(), self.history_store,
                                      max_input_tokens=99000, max_summary_tokens=1000)
        return ContextManager(config or ContextConfig(100000, 1000), compactor, [], self.manager)

    def history(self):
        agent = Agent(Replies([Message(Role.ASSISTANT, "任务一完成")]), ToolRegistry(), lambda _: False,
                      store=self.history_store)
        agent.run("旧任务：" + "已有历史资料。" * 200)
        second = Agent(Replies([Message(Role.ASSISTANT, "任务二完成")]), ToolRegistry(), lambda _: False,
                       store=self.history_store)
        second.run("当前真实任务", history=agent.history_tree)
        return second.history_tree, second._task.id

    def test_empty_and_new_memory_is_not_applied(self):
        self.assertIsNone(self.manager.get_active_memory())
        self.create()
        self.assertIsNone(self.manager.get_active_memory())
        history, task_id = self.history()
        view = self.context().inspect(history, task_id)
        self.assertEqual(memory_messages(view.messages), [])
        self.assertEqual(len(view.messages), len(history.path()))

    def test_only_active_memory_at_tail_and_history_unchanged(self):
        self.create("未应用", "不要注入")
        active = self.create(content="真正记忆", apply=True)
        history, task_id = self.history()
        original = history_to_dict(history)
        manager = self.context()
        for _ in range(3):
            view = manager.inspect(history, task_id)
            self.assertEqual(len(memory_messages(view.messages)), 1)
            self.assertEqual(json.loads(view.messages[-1].content)["content"], "真正记忆")
            self.assertEqual(view.messages[-1].role, Role.USER)
            self.assertEqual(view.memory_id, active)
            self.assertEqual(view.messages[0].content.count(MEMORY_RULES), 1)
        self.assertEqual(history_to_dict(history), original)

    def test_switch_changes_next_context_tokens_and_fingerprint(self):
        first = self.create(content="短", apply=True)
        second = self.create("另一条", "更多长期资料。" * 40)
        history, task_id = self.history()
        context = self.context()
        before = context.inspect(history, task_id)
        self.manager.execute("apply", memory_id=second)
        after = context.inspect(history, task_id)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertGreater(after.estimate.estimated_input_tokens, before.estimate.estimated_input_tokens)
        self.assertEqual(before.memory_id, first)
        self.assertEqual(after.memory_id, second)

    def test_update_delete_restart_and_conversation_scan(self):
        active = self.create(apply=True)
        other = self.create("另一条", "保留")
        self.manager.execute("update", memory_id=active, name="修改后", content="# 新内容")
        restarted = PersistentMemoryManager(PersistentMemoryStore(self.root / "memory"))
        self.assertEqual(restarted.get_active_memory().content, "# 新内容")
        restarted.execute("delete", memory_id=active)
        again = PersistentMemoryManager(PersistentMemoryStore(self.root / "memory"))
        self.assertIsNone(again.get_active_memory())
        self.assertEqual(again.list_memories()["memories"][0]["id"], other)
        self.assertEqual(self.history_store.list_conversations(), ([], []))

    def test_model_context_reads_cache_not_disk(self):
        self.create(apply=True)
        history, task_id = self.history()
        context = self.context()
        with patch.object(self.manager.store, "load", side_effect=AssertionError("must not read disk")):
            for _ in range(3):
                self.assertEqual(len(memory_messages(context.inspect(history, task_id).messages)), 1)

    def test_failed_atomic_replace_keeps_cache_and_file(self):
        active = self.create(apply=True)
        before = self.manager.list_memories()
        disk = self.manager.store.path.read_bytes()
        with patch("backend.memory_store.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.manager.execute("delete", memory_id=active)
        self.assertEqual(before, self.manager.list_memories())
        self.assertEqual(disk, self.manager.store.path.read_bytes())
        self.assertEqual(list(self.manager.store.path.parent.glob("*.tmp")), [])

    def test_concurrent_revision_check_and_request_deduplication(self):
        def create(name):
            try:
                return self.manager.execute("create", name=name, content="", expected_revision=0)
            except MemoryConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["A", "B"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        result = self.manager.execute("create", name="幂等", content="", expected_revision=1, request_id="req-1")
        self.assertEqual(self.manager.execute("create", name="幂等", content="", expected_revision=1, request_id="req-1"), result)
        self.assertEqual(len(self.manager.list_memories()["memories"]), 2)
        with self.assertRaises(MemoryConflict):
            self.manager.execute("create", name="不同内容", content="", request_id="req-1")

    def test_second_writer_cannot_overwrite_new_file(self):
        stale = PersistentMemoryManager(PersistentMemoryStore(self.root / "memory"))
        self.create()
        with self.assertRaises(MemoryConflict):
            stale.execute("create", name="旧实例", content="")
        self.assertEqual(len(self.manager.list_memories()["memories"]), 1)

    def test_invalid_catalog_and_input_are_rejected(self):
        with self.assertRaises(ValueError):
            self.create(name=" ")
        with self.assertRaises(ValueError):
            self.create(content=False)
        path = self.manager.store.path
        path.parent.mkdir(parents=True)
        for text in ('{bad', '{"version":1,"active_memory_id":"missing","memories":[]}',
                     '{"version":2,"active_memory_id":null,"memories":[]}'):
            path.write_text(text)
            with self.assertRaises(ValueError):
                PersistentMemoryManager(PersistentMemoryStore(self.root / "memory"))
            self.assertEqual(path.read_text(), text)

    def test_compaction_retains_tail_memory_and_excludes_it_from_summary(self):
        self.create(content="MEMORY_ONLY_SENTINEL", apply=True)
        history, task_id = self.history()
        nodes_before = {key: (node.parent_id, list(node.children_ids), node.message.content)
                        for key, node in history.nodes.items()}
        summary = Summarizer()
        view = self.context(summary).prepare(history, task_id, force_compact=True)
        self.assertIsNotNone(view.compaction_id)
        self.assertEqual(memory_messages(view.messages)[0]["content"], "MEMORY_ONLY_SENTINEL")
        self.assertNotIn("MEMORY_ONLY_SENTINEL", str(summary.requests))
        self.assertEqual(nodes_before, {key: (node.parent_id, list(node.children_ids), node.message.content)
                                       for key, node in history.nodes.items()})

    def test_compaction_uses_one_snapshot_even_when_memory_changes(self):
        self.create(content="A", apply=True)
        next_id = self.create("B", "B" * 100)
        history, task_id = self.history()
        summary = Summarizer(lambda: self.manager.execute("apply", memory_id=next_id))
        context = self.context(summary)
        view = context.prepare(history, task_id, force_compact=True)
        self.assertEqual(memory_messages(view.messages)[0]["content"], "A")
        self.assertEqual(memory_messages(context.inspect(history, task_id).messages)[0]["content"], "B" * 100)

    def test_memory_that_cannot_fit_reports_context_error(self):
        self.create(content="过长资料。" * 300, apply=True)
        history, task_id = self.history()
        with self.assertRaises(ModelError) as error:
            self.context(config=ContextConfig(2000, 100, safety_tokens=0)).prepare(history, task_id)
        self.assertEqual(error.exception.kind, ErrorKind.CONTEXT_LIMIT)

    def test_transient_retry_uses_latest_memory(self):
        self.create(content="A", apply=True)
        next_id = self.create("B", "B")
        def failed():
            self.manager.execute("apply", memory_id=next_id)
            return ModelError("temporary", kind=ErrorKind.TRANSIENT)
        client = Replies([failed, Message(Role.ASSISTANT, "完成")])
        agent = Agent(client, ToolRegistry(), lambda _: False, store=self.history_store,
                      context_manager=self.context(), retry_policy=RetryPolicy(base_delay=0))
        result = agent.run("真实任务")
        self.assertEqual(result.status, "completed")
        self.assertEqual([memory_messages(request)[0]["content"] for request in client.requests], ["A", "B"])
        self.assertNotEqual(agent.request_reports[0].context_fingerprint, agent.request_reports[1].context_fingerprint)

    def test_provider_context_limit_forces_compaction_and_keeps_memory(self):
        self.create(content="MEMORY_RETRY", apply=True)
        history, _ = self.history()
        client = Replies([ModelError("context limit", kind=ErrorKind.CONTEXT_LIMIT),
                          Message(Role.ASSISTANT, "恢复成功")])
        agent = Agent(client, ToolRegistry(), lambda _: False, store=self.history_store,
                      context_manager=self.context())
        self.assertEqual(agent.run("新任务", history=history).status, "completed")
        self.assertEqual(len(client.requests), 2)
        self.assertLess(len(client.requests[1]), len(client.requests[0]))
        self.assertNotEqual(agent.request_reports[0].context_fingerprint, agent.request_reports[1].context_fingerprint)
        for request in client.requests:
            self.assertEqual(memory_messages(request), [{"context_type": "long_term_memory",
                                                        "name": "项目约定", "content": "MEMORY_RETRY"}])

    def test_api_crud_csrf_revision_and_read_only(self):
        class Service:
            memory_manager = None
            def snapshot(self):
                return {"execution_connected": True, "busy": True}
        app = create_app(store=self.history_store, service=Service(), memory_manager=self.manager,
                         context_config=ContextConfig(100000, 1000))
        client = app.test_client()
        csrf = client.get("/api/session").get_json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Origin": "http://localhost"}
        sequence = 0
        def change(method, path, **data):
            nonlocal sequence
            sequence += 1
            return client.open(path, method=method, json={"client_request_id": f"req-{sequence}",
                "expected_memory_revision": self.manager.get_snapshot().revision, **data}, headers=headers)
        self.assertEqual(client.post("/api/memories", json={}).status_code, 403)
        result = change("POST", "/api/memories", name="API", content="# Markdown")
        self.assertEqual(result.status_code, 201)
        memory_id = result.get_json()["memory_id"]
        self.assertIsNone(self.manager.get_active_memory())
        self.assertEqual(change("POST", "/api/memories/active", memory_id=memory_id).status_code, 200)
        self.assertEqual(client.put(f"/api/memories/{memory_id}", json={}).status_code, 403)
        self.assertEqual(change("PUT", f"/api/memories/{memory_id}", name="修改", content="新内容").status_code, 200)
        self.assertEqual(client.delete(f"/api/memories/{memory_id}", json={}).status_code, 403)
        self.assertEqual(change("DELETE", f"/api/memories/{memory_id}").status_code, 200)
        self.assertIsNone(client.get("/api/memories").get_json()["active_memory_id"])
        self.assertEqual(client.post("/api/memories", json={"client_request_id": "stale", "expected_memory_revision": 0,
                         "name": "旧编辑", "content": ""}, headers=headers).status_code, 409)
        readonly = create_app(store=self.history_store, memory_manager=self.manager,
                              context_config=ContextConfig(100000, 1000)).test_client()
        self.assertFalse(readonly.get("/api/memories").get_json()["writable"])
        self.assertEqual(readonly.post("/api/memories", json={}).status_code, 405)

    def test_preview_includes_memory_without_changing_history_revision(self):
        memory_id = self.create(content="短记忆", apply=True)
        history, _ = self.history()
        before = conversation_snapshot(history, config=ContextConfig(100000, 1000),
                                       memory_snapshot=self.manager.get_snapshot())
        self.manager.execute("update", memory_id=memory_id, name="长记忆", content="更多资料。" * 100)
        after = conversation_snapshot(history, config=ContextConfig(100000, 1000),
                                      memory_snapshot=self.manager.get_snapshot())
        self.assertEqual(before["history_revision"], after["history_revision"])
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertGreater(after["metrics"]["estimated_input_tokens"], before["metrics"]["estimated_input_tokens"])
        self.assertEqual(after["metrics"]["memory_name"], "长记忆")
        self.assertEqual(memory_messages([entry.message for entry in history.path()]), [])

    def test_worker_switch_while_awaiting_approval_and_real_tool_round_trip(self):
        self.create(content="memory-A", apply=True)
        next_id = self.create("另一条", "memory-B")
        with AgentService(directory=self.history_store.directory, root=self.root, factory=worker_factory,
                          memory_manager=self.manager) as service:
            self.assertTrue(service.snapshot()["execution_connected"])
            conversation = service.call("create", {"client_request_id": "create"})["conversation_id"]
            history = self.history_store.load(conversation)
            service.call("submit", {"client_request_id": "submit", "conversation_id": conversation,
                "expected_revision": history_revision(history), "text": "执行工具后继续"})
            deadline = time.monotonic() + 8
            approval = None
            while time.monotonic() < deadline:
                approval = service.details(conversation)["pending_approval"]
                if approval:
                    break
                time.sleep(.02)
            self.assertIsNotNone(approval)
            # 记忆操作在 HTTP 父进程执行，不受 Worker 等待审批的 busy 状态阻塞。
            self.manager.execute("apply", memory_id=next_id)
            self.assertEqual(history_revision(self.history_store.load(conversation)), approval["history_revision"])
            service.call("approval", {"client_request_id": "approve", "conversation_id": conversation,
                "task_id": approval["task_id"], "approval_id": approval["id"],
                "tool_call_id": approval["tool_call_id"], "decision": "approve",
                "expected_revision": approval["history_revision"]})
            while service.snapshot()["busy"] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(service.snapshot()["busy"])
            history = self.history_store.load(conversation)
            self.assertEqual(history.path()[-1].message.content, "memory-B")
            self.assertEqual(len(history.tasks), 1)
            self.assertEqual(memory_messages([entry.message for entry in history.path()]), [])


if __name__ == "__main__":
    unittest.main()
