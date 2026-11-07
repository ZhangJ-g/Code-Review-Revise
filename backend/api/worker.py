"""独立、单线程的 Agent 服务进程；所有写操作串行处理，审批通过 Pipe 等待。

不使用 Queue，避免后台发送线程进入现有 fork 执行器。IPC 只接受父进程的
已校验命令；浏览器不能指定 Python 函数、执行代码或 Agent 工厂。
"""

import hashlib
import secrets
from dataclasses import asdict
from pathlib import Path

from ..agent import Agent
from ..agent_factory import create_agent, create_registry
from ..history import History
from ..memory_store import JsonMemoryStore
from ..message_utils import pending_tool_calls
from ..model_api import ModelError
from ..models import MemorySnapshot
from ..prompts import build_system_message
from ..runtime_lock import ProjectBusyError, ProjectLock
from .errors import ServiceError
from .serializers import history_revision


class _ObservedStore(JsonMemoryStore):
    """提交成功后发观测事件；通知失败不能伪装成检查点保存失败。"""

    def __init__(self, directory, observer):
        super().__init__(directory)
        self.observer = observer
        self.versions = {}

    def current_revision(self, conversation_id):
        return history_revision(super().load(conversation_id))

    def load(self, conversation_id):
        history = super().load(conversation_id)
        self.versions[conversation_id] = history_revision(history)
        return history

    def save(self, history):
        conversation_id = history.conversation.id
        expected = self.versions.get(conversation_id)
        try:
            current = self.current_revision(conversation_id)
        except FileNotFoundError:
            current = None
        except (ValueError, OSError):
            raise OSError("原历史无法校验，已停止保存；请重新加载磁盘状态") from None
        if current != expected:
            raise OSError("历史已被其他程序修改或移走，已停止保存；请重新加载磁盘状态")
        target = super().save(history)
        self.versions[conversation_id] = history_revision(history)
        try:
            self.observer(history)
        except Exception:
            pass
        return target


class _NoModel:
    def complete(self, messages):
        raise RuntimeError("任务控制操作不能请求模型")


class _Worker:
    def __init__(self, connection, directory, root, factory):
        self.connection, self.root, self.factory = connection, Path(root), factory
        self.store = _ObservedStore(directory, self.saved)
        self.command = None
        self.agent = None
        self.task_id = None
        self.acknowledged = False
        self.disconnected = False

    def send(self, message):
        try:
            self.connection.send(message)
            return True
        except (EOFError, OSError):
            self.disconnected = True
            return False

    def event(self, name, data):
        return self.send({"type": "event", "name": name, "data": data})

    def reply(self, command, *, result=None, error=None):
        self.send({"type": "reply", "id": command["id"], "result": result,
                   "error": error.to_dict() if error is not None else None})

    def observations(self):
        if self.agent is None:
            return {}
        reports = self.agent.request_reports
        view = self.agent.last_round_context
        return {
            "task_id": self.task_id,
            "latest_request": asdict(reports[-1]) if reports else None,
            "last_round_context": ({"estimated_input_tokens": view.estimate.estimated_input_tokens,
                                    "compaction_id": view.compaction_id} if view else None),
            "statistics_error": self.agent.last_round_error,
        }

    def saved(self, history):
        if self.agent is None:
            return
        if self.task_id is None:
            task = history.unfinished_task()
            if task is not None:
                self.task_id = task.id
        task = history.tasks.get(self.task_id)
        if task is None:
            return
        data = {"conversation_id": history.conversation.id, "task_id": task.id,
                "checkpoint_node_id": task.checkpoint_node_id, "phase": task.phase,
                "status": task.status, "history_revision": history_revision(history),
                "observation": self.observations()}
        self.event("checkpoint", data)
        if not self.acknowledged and self.command["method"] in {"submit", "resume"}:
            self.reply(self.command, result={"conversation_id": history.conversation.id,
                                            "task_id": task.id, "accepted": True})
            self.acknowledged = True  # 首次持久化之后才返回 202；不等待模型完成。

    def observe(self, name, data):
        self.event(name, {"conversation_id": self.command["data"].get("conversation_id"),
                          "task_id": self.task_id, **data})

    def get_snapshot(self) -> MemorySnapshot:
        """向父进程请求当前记忆状态，并等待对应回复。

        Worker 不保存另一份记忆目录。通信失败就报错，不能当作用户关闭了记忆。
        """
        # 通信编号：确认收到的回复属于这次请求。
        request_id = secrets.token_hex(12)
        if not self.send({"type": "memory_snapshot_request", "id": request_id}):
            raise RuntimeError("长期记忆服务连接已中断")
        if not self.connection.poll(10):
            raise RuntimeError("长期记忆快照获取超时，未发送模型请求")
        try:
            response = self.connection.recv()
        except (EOFError, OSError):
            raise RuntimeError("长期记忆服务连接已中断") from None
        if response.get("method") == "shutdown":
            raise KeyboardInterrupt
        if (response.get("type") != "memory_snapshot" or response.get("id") != request_id
                or not isinstance(response.get("snapshot"), MemorySnapshot)):
            raise RuntimeError("长期记忆快照响应无效，未发送模型请求")
        return response["snapshot"]

    def approve(self, code):
        if self.disconnected:
            raise KeyboardInterrupt
        history = self.agent.history_tree
        task = history.tasks[self.task_id]
        calls = pending_tool_calls([entry.message for entry in history.path()])
        call = next((item for item in calls if item.id == task.active_tool_call_id), None)
        if (call is None or task.phase != "waiting_approval" or call.name != "run_code"
                or call.arguments.get("code") != code):
            raise ValueError("审批请求与当前工具检查点不一致")
        approval = {
            "id": secrets.token_urlsafe(24), "conversation_id": history.conversation.id,
            "task_id": task.id, "tool_call_id": call.id, "checkpoint_node_id": task.checkpoint_node_id,
            "code": code, "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "history_revision": history_revision(history),
        }
        if not self.event("approval_requested", approval):
            raise KeyboardInterrupt
        while True:
            try:
                command = self.connection.recv()
            except (EOFError, OSError):
                raise KeyboardInterrupt from None
            if command["method"] == "shutdown":
                raise KeyboardInterrupt
            try:
                data = command["data"]
                if command["method"] != "approval":
                    raise ServiceError("任务正在等待审批，暂不能处理其他写操作。")
                checks = {"approval_id": approval["id"], "conversation_id": approval["conversation_id"],
                          "task_id": task.id, "tool_call_id": call.id,
                          "expected_revision": approval["history_revision"]}
                if any(data.get(key) != value for key, value in checks.items()):
                    raise ServiceError("审批已失效，请刷新后核对当前代码。", code="stale_approval")
                if self.store.current_revision(history.conversation.id) != approval["history_revision"]:
                    raise ServiceError("历史在审批期间发生变化，未执行代码；请重新加载。", code="stale_history")
                if not isinstance(data.get("decision"), str) or data["decision"] not in {"approve", "reject"}:
                    raise ServiceError("审批决定无效。", code="invalid_request", status=400)
                self.reply(command, result={"accepted": True, "task_id": task.id})
                self.event("approval_resolved", {"task_id": task.id, "approval_id": approval["id"]})
                return data["decision"] == "approve"
            except ServiceError as error:
                self.reply(command, error=error)

    def execute(self, command):
        self.command, self.agent = command, None
        self.task_id, self.acknowledged = command["data"].get("task_id"), False
        method, data = command["method"], command["data"]
        if method == "create":
            history = History.create(build_system_message(create_registry(self.root)))
            self.store.save(history)
            return {"conversation_id": history.conversation.id}

        history = self.store.load(data["conversation_id"])
        if history_revision(history) != data["expected_revision"]:
            raise ServiceError("历史版本已更新，请刷新后重新确认操作。", code="stale_history")
        if method == "select":
            history.select_leaf(data["leaf_id"])
            self.store.save(history)
            return {"conversation_id": history.conversation.id, "leaf_id": data["leaf_id"]}

        if method in {"submit", "resume"}:
            self.agent = self.factory(self.approve, store=self.store, root=self.root, observer=self.observe)
            # 让 Agent 通过上面的 get_snapshot() 向父进程取记忆。
            self.agent.set_memory_manager(self)
        else:
            self.agent = Agent(_NoModel(), create_registry(self.root), self.approve, store=self.store)

        if method == "submit":
            result = self.agent.run(data["text"], history=history, parent_id=data.get("parent_id"))
        elif method == "resume":
            if data.get("confirmed_stopped") is not True:
                raise ServiceError("恢复前请确认旧程序和工具子进程已停止。", code="confirmation_required", status=400)
            result = self.agent.resume(history, data["task_id"])
        elif method == "review":
            self.agent.acknowledge_unknown(history, data["task_id"], data["note"],
                                           confirmed_stopped=data["confirmed_stopped"])
            return {"task_id": data["task_id"], "recorded": True}
        elif method == "cancel":
            result = self.agent.cancel_task(history, data["task_id"])
        else:
            raise ServiceError("不支持的操作。", code="invalid_request", status=400)
        return asdict(result)

    def run(self):
        while not self.disconnected:
            try:
                command = self.connection.recv()
            except (EOFError, OSError, KeyboardInterrupt):
                return
            if command["method"] == "shutdown":
                return
            result, error, interrupted = None, None, False
            try:
                result = self.execute(command)
            except ServiceError as caught:
                error = caught
            except ModelError as caught:
                error = ServiceError(str(caught), code=caught.kind.value, status=422)
            except (ValueError, KeyError) as caught:
                error = ServiceError(str(caught) if isinstance(caught, ValueError) else "操作缺少必要字段。",
                                     code="invalid_request", status=400)
            except FileNotFoundError:
                error = ServiceError("对话不存在，请刷新列表。", code="not_found", status=404)
            except OSError:
                error = ServiceError("历史读取或保存失败，请重新加载磁盘状态；已有文件修改不会回滚。",
                                     code="storage_error", status=503)
            except KeyboardInterrupt:
                error = ServiceError("任务已中断，请重新加载检查点后恢复。", code="interrupted")
                interrupted = True
            except Exception:
                error = ServiceError("任务执行异常，请重新加载检查点核对状态。", code="execution_error", status=500)
            self.event("operation_done", {"conversation_id": command["data"].get("conversation_id"),
                                           "task_id": self.task_id, "result": result,
                                           "history_revision": self.store.versions.get(command["data"].get("conversation_id")),
                                           "error": error.to_dict() if error else None,
                                           "observation": self.observations()})
            if not self.acknowledged:
                self.reply(command, result=result, error=error)
            if interrupted:
                return


def worker_main(connection, directory, root, lock_path, factory=create_agent):
    """spawn 的可导入入口；后台进程持有锁，HTTP 退出时锁不会提前释放。"""
    try:
        with ProjectLock(Path(lock_path), owner="Web Agent"):
            connection.send({"type": "ready", "ready": True, "message": None})
            _Worker(connection, directory, root, factory).run()
    except ProjectBusyError as error:
        connection.send({"type": "ready", "ready": False, "message": str(error)})
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    finally:
        connection.close()
