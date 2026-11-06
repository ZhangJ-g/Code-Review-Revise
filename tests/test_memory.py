"""使用临时目录与模拟模型验证历史树，不访问真实服务。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agent import Agent
from backend.history import History
from backend.memory_store import JsonMemoryStore, history_from_dict, history_to_dict
from backend.models import Message, Role, ToolCall
from .test_agent import ScriptedClient
from backend.tools import ToolRegistry, register_file_tools


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JsonMemoryStore(Path(self.directory.name) / "memory")

    def make_history(self):
        history = History.create(Message(Role.SYSTEM, "系统说明"))
        user = history.append(Message(Role.USER, "检查排序"))
        answer = history.append(Message(Role.ASSISTANT, "原分支分析"))
        return history, user, answer

    def test_branch_save_reload_and_continue_conversation(self):
        registry = ToolRegistry()
        first = Agent(#first agent
            # 模拟ai回答：第一轮回答
            ScriptedClient([Message(Role.ASSISTANT, "第一轮的回答xxx")]),
            registry, lambda code: False, store=self.store,
        )
        # self.assertEqual(实际结果, 预期结果)
        self.assertEqual(first.run("第一轮的任务请求xxx").status, "completed")
        history = first.history_tree
        conversation_id = history.conversation.id
        fork_point = history.conversation.current_leaf_id

        # 先产生分支 A，并确认已经自动保存。
        branch_a = Agent(
            ScriptedClient([Message(Role.ASSISTANT, "分支 A 回答")]),
            registry, lambda code: False, store=self.store,
        )
        branch_a.run("分支 A 任务", history=history)
        branch_a_leaf = history.conversation.current_leaf_id

        # 重新读取文件后，从第一轮回答创建分支 B，保留 A。
        loaded = JsonMemoryStore(self.store.directory).load(conversation_id)
        branch_b = Agent(
            ScriptedClient([Message(Role.ASSISTANT, tool_calls=[
                ToolCall("run_code", {"code": 'print("分支 B 工具结果")'}, "branch_b_call"),
            ]), Message(Role.ASSISTANT, "分支 B 回答")]),
            registry, lambda code: True, store=self.store,
        )
        self.assertEqual(branch_b.run("分支 B 任务", history=loaded, parent_id=fork_point).status, "completed")
        branch_b_leaf = loaded.conversation.current_leaf_id
        self.assertEqual(len(loaded.nodes[fork_point].children_ids), 2)
        self.assertIn(branch_a_leaf, loaded.nodes)

        # 再次重建存储器和 Agent，继续当前分支 B。
        reloaded = JsonMemoryStore(self.store.directory).load(conversation_id)
        client = ScriptedClient([Message(Role.ASSISTANT, "B 分支的后续回答")])
        continued = Agent(client, registry, lambda code: False, store=self.store)
        self.assertEqual(continued.run("继续讨论 B", history=reloaded).status, "completed")
        context = "\n".join(message.content for message in client.requests[0])
        self.assertIn("第一轮任务", context)
        self.assertIn("分支 B 任务", context)
        self.assertIn("分支 B 工具结果", context)
        self.assertNotIn("分支 A 任务", context)
        self.assertNotIn("分支 A 回答", context)
        self.assertEqual(continued.history[-2].parent_id, branch_b_leaf)

        final = self.store.load(conversation_id)
        self.assertIn(branch_a_leaf, final.nodes)
        final.select_leaf(branch_a_leaf)
        self.assertEqual(final.path()[-1].message.content, "分支 A 回答")
        self.store.save(final)
        self.assertEqual(self.store.load(conversation_id).conversation.current_leaf_id, branch_a_leaf)
        self.assertEqual(len(list(self.store.directory.glob("*.json"))), 1)

    def test_round_trip_preserves_ids_timestamps_and_tool_messages(self):
        history, _, _ = self.make_history()
        call = ToolCall("run_code", {"code": 'print("中文")'}, "tool_1")
        history.append(Message(Role.ASSISTANT, tool_calls=[call]))
        history.append(Message(Role.TOOL, "中文结果", tool_call_id=call.id))
        original = history_to_dict(history)
        path = self.store.save(history)
        self.assertIn("中文结果", path.read_text(encoding="utf-8"))
        loaded = self.store.load(history.conversation.id)
        self.assertEqual(history_to_dict(loaded), original)
        self.assertIsInstance(loaded.path()[-1].message.role, Role)
        self.assertEqual(loaded.path()[-2].message.tool_calls[0], call)
        self.assertEqual(loaded.path()[-1].created_at, history.path()[-1].created_at)

    def test_rejects_invalid_graphs_and_leaf(self):
        history, _, _ = self.make_history()
        good = history_to_dict(history)

        def duplicate_id(data):
            data["nodes"].append(copy.deepcopy(data["nodes"][0]))

        def invalid_parent(data):
            data["nodes"][1]["parent_id"] = "missing"

        def broken_backlink(data):
            data["nodes"][0]["children_ids"] = []

        def missing_child(data):
            data["nodes"][0]["children_ids"].append("missing")

        def duplicate_child(data):
            data["nodes"][0]["children_ids"] *= 2

        def non_leaf(data):
            data["conversation"]["current_leaf_id"] = data["nodes"][0]["id"]

        def wrong_root(data):
            data["conversation"]["root_id"] = data["nodes"][1]["id"]

        def missing_leaf(data):
            data["conversation"]["current_leaf_id"] = "missing"

        def two_roots(data):
            data["nodes"][0]["children_ids"] = []
            data["nodes"][1]["parent_id"] = None

        def detached_cycle(data):
            root, user, assistant = data["nodes"]
            root["children_ids"] = []
            user["parent_id"] = assistant["id"]
            assistant["children_ids"] = [user["id"]]
            data["conversation"]["current_leaf_id"] = root["id"]

        for mutate in (duplicate_id, invalid_parent, broken_backlink, missing_child,
                       duplicate_child, non_leaf, wrong_root, missing_leaf, two_roots, detached_cycle):
            with self.subTest(case=mutate.__name__):
                data = copy.deepcopy(good)
                mutate(data)
                with self.assertRaises(ValueError):
                    history_from_dict(data)

    def test_rejects_invalid_json_fields_and_filename(self):
        history, _, _ = self.make_history()
        good = history_to_dict(history)
        for path, value in [
            (("schema_version",), 2),
            (("conversation", "created_at"), "2026-10-29T00:00:00"),
            (("conversation", "updated_at"), "2000-01-01T00:00:00+00:00"),
            (("nodes", 0, "message", "role"), "unknown"),
            (("nodes", 0, "message", "content"), 10),
        ]:
            with self.subTest(path=path):
                data = copy.deepcopy(good)
                target = data
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    history_from_dict(data)
        for field in ("id", "root_id", "current_leaf_id"):
            data = copy.deepcopy(good)
            del data["conversation"][field]
            with self.assertRaises(ValueError):
                history_from_dict(data)
        target = self.store.save(history)
        data = copy.deepcopy(good)
        data["conversation"]["id"] = "another-conversation"
        target.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "文件名"):
            self.store.load(history.conversation.id)
        for text in ("not json", '{"schema_version": 1, "schema_version": 1}', '{"value": NaN}'):
            target.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.store.load(history.conversation.id)
        for bad_id in ("../escape", "/tmp/escape", "", "a/b"):
            with self.assertRaises(ValueError):
                self.store.load(bad_id)

    def test_failed_replace_keeps_previous_snapshot(self):
        history, _, _ = self.make_history()
        target = self.store.save(history)
        original = target.read_bytes()
        history.append(Message(Role.USER, "未保存的新消息"))
        with patch("backend.memory_store.os.replace", side_effect=OSError("模拟写盘失败")):
            with self.assertRaises(OSError):
                self.store.save(history)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])

    def test_pending_tool_call_is_saved_but_never_replayed(self):
        history, user, _ = self.make_history()
        history.append(Message(Role.ASSISTANT, tool_calls=[
            ToolCall("run_code", {"code": 'print("must not run")'}, "pending_1"),
        ]))
        self.store.save(history)
        loaded = self.store.load(history.conversation.id)
        node_count = len(loaded.nodes)
        client = ScriptedClient([Message(Role.ASSISTANT, "从完整节点继续")])
        agent = Agent(client, ToolRegistry(), lambda code: self.fail("不能重放"), store=self.store)
        with self.assertRaisesRegex(ValueError, "未完成的工具调用"):
            agent.run("继续", history=loaded)
        self.assertEqual(len(client.requests), 0)
        self.assertEqual(len(loaded.nodes), node_count)
        # 可以明确从更早的完整节点分支，原有未完成分支继续保留。
        self.assertEqual(agent.run("从旧节点重新分析", history=loaded, parent_id=user.id).status, "completed")

    def test_save_failure_prevents_tool_execution(self):
        client = ScriptedClient([Message(Role.ASSISTANT, tool_calls=[
            ToolCall("run_code", {"code": 'print("should not execute")'}, "call_1"),
        ])])
        agent = Agent(client, ToolRegistry(), lambda code: self.fail("保存失败后不应执行"), store=self.store)
        original_save = self.store.save

        def failing_save(history):
            if history.path()[-1].message.tool_calls:
                raise OSError("模拟工具调用落盘失败")
            return original_save(history)

        with patch.object(self.store, "save", side_effect=failing_save):
            with self.assertRaises(OSError):
                agent.run("执行测试")

    def test_feedback_save_failure_does_not_replay_completed_side_effect(self):
        root = Path(self.directory.name)
        registry = ToolRegistry()
        register_file_tools(registry, root)
        client = ScriptedClient([Message(Role.ASSISTANT, tool_calls=[
            ToolCall("run_code", {"code": 'write_file("created.txt", "已执行")'}, "call_1"),
        ])])
        agent = Agent(client, registry, lambda code: True, store=self.store)
        original_save = self.store.save

        def failing_feedback_save(history):
            if history.path()[-1].message.role == Role.TOOL:
                raise OSError("模拟结果落盘失败")
            return original_save(history)

        with patch.object(self.store, "save", side_effect=failing_feedback_save):
            with self.assertRaises(OSError):
                agent.run("新建临时文件")
        self.assertEqual((root / "created.txt").read_text(), "已执行")
        loaded = self.store.load(agent.history_tree.conversation.id)
        self.assertEqual(loaded.path()[-1].message.role, Role.ASSISTANT)
        resumed = Agent(
            ScriptedClient([]), registry, lambda code: self.fail("不能重放"), store=self.store,
        )
        with self.assertRaisesRegex(ValueError, "未完成的工具调用"):
            resumed.run("继续", history=loaded)


if __name__ == "__main__":
    unittest.main()
