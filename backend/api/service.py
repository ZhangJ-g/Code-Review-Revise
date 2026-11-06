"""HTTP 进程的运行适配器：管理单个 spawn 进程、审批及请求去重。

运行状态只用于展示和调度；消息与 TaskRun 恢复仍以磁盘检查点为准。
父进程的接收线程不执行 Agent，后台 Agent 进程本身始终单线程。
"""

import copy
import hashlib
import json
import multiprocessing
import os
import secrets
import signal
import threading
from collections import OrderedDict
from pathlib import Path

from ..agent_factory import create_agent
from ..paths import MEMORY_DIR, PROJECT_ROOT
from .errors import ServiceError
from .worker import worker_main


class _Receipt:
    def __init__(self, method, fingerprint):
        self.method, self.fingerprint = method, fingerprint
        self.event = threading.Event()
        self.result = self.error = None


class AgentService:
    def __init__(self, *, directory=MEMORY_DIR, root=PROJECT_ROOT, factory=create_agent):
        self.directory, self.root, self.factory = Path(directory).resolve(), Path(root).resolve(), factory
        self._mutex = threading.RLock()
        self._ready_event = threading.Event()
        self._process = self._connection = self._reader = None
        self._ready = self._closed = self._busy = self._approval_sending = False
        self._message = None
        self._active = {"conversation_id": None, "task_id": None, "phase": "idle"}
        self._pending_approval = None
        self._observations = {}
        self._receipts = OrderedDict()
        self._last_error = None
        self._version = 0
        self._instance_id = secrets.token_hex(12)

    def start(self):
        if self._process is not None:
            return self
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        lock_path = self.directory.parent / ".runtime" / "agent.lock"
        self._connection = parent
        self._process = context.Process(target=worker_main,
                                        args=(child, str(self.directory), str(self.root), str(lock_path), self.factory),
                                        daemon=False, name="code-agent-worker")
        try:
            self._process.start()
        except BaseException:
            parent.close()
            child.close()
            self._closed = True
            raise
        child.close()
        self._reader = threading.Thread(target=self._receive, daemon=True, name="agent-observer")
        self._reader.start()
        if not self._ready_event.wait(10):
            self.close()
            raise ServiceError("后台进程启动超时，未执行任务。", code="startup_timeout", status=503)
        return self

    def _observe(self, conversation_id, observation):
        if not conversation_id or not observation.get("task_id"):
            return
        previous = self._observations.get(conversation_id, {})
        if previous.get("task_id") != observation["task_id"]:
            previous = {}
        for key, value in observation.items():
            if value is not None or key not in previous:
                previous[key] = value
        self._observations[conversation_id] = previous

    def _receive(self):
        try:
            while True:
                message = self._connection.recv()
                with self._mutex:
                    self._version += 1
                    if message["type"] == "ready":
                        self._ready, self._message = message["ready"], message["message"]
                        self._ready_event.set()
                    elif message["type"] == "reply":
                        receipt = self._receipts.get(message["id"])
                        if receipt is None:
                            continue
                        receipt.result, receipt.error = message["result"], message["error"]
                        if receipt.method == "approval":
                            self._approval_sending = False
                            if receipt.error is None:
                                self._pending_approval = None
                        receipt.event.set()
                    elif message["type"] == "event":
                        name, data = message["name"], message["data"]
                        if name == "checkpoint":
                            self._active = {key: data[key] for key in ("conversation_id", "task_id", "phase", "history_revision")}
                            self._observe(data["conversation_id"], data["observation"])
                        elif name == "approval_requested":
                            self._pending_approval = data
                            self._active["phase"] = "waiting_approval"
                        elif name == "approval_resolved":
                            self._pending_approval = None
                            self._approval_sending = False
                        elif name.startswith("compaction_"):
                            self._active["phase"] = "compacting" if name == "compaction_started" else "waiting_model"
                        elif name == "operation_done":
                            self._observe(data["conversation_id"], data["observation"])
                            self._busy = self._approval_sending = False
                            self._pending_approval = None
                            self._last_error = data["error"]
                            self._active = {"conversation_id": data["conversation_id"],
                                            "task_id": data["task_id"], "phase": "idle",
                                            "history_revision": data.get("history_revision")}
        except (EOFError, OSError, TypeError):
            pass
        finally:
            with self._mutex:
                self._version += 1
                self._ready = self._busy = self._approval_sending = False
                self._pending_approval = None
                if not self._closed and not self._message:
                    self._message = "后台进程已退出；请重启 Web 服务，再从检查点恢复任务。"
                self._ready_event.set()
                for receipt in self._receipts.values():
                    if not receipt.event.is_set():
                        receipt.error = {"code": "worker_lost", "status": 503,
                                         "message": "后台连接中断，请刷新检查点；不要重复提交结果未知的操作。"}
                        receipt.event.set()

    def snapshot(self):
        with self._mutex:
            alive = bool(self._process is not None and not self._closed and self._process.is_alive())
            connected = bool(alive and self._ready)
            return copy.deepcopy({
                "execution_connected": connected, "worker_alive": alive, "busy": self._busy and connected,
                **self._active, "message": self._message, "last_error": self._last_error,
                "source": "managed_worker", "has_pending_approval": self._pending_approval is not None,
                "version": self._version,
                "instance_id": self._instance_id,
            })

    def details(self, conversation_id):
        with self._mutex:
            approval = self._pending_approval
            return copy.deepcopy({"runtime": self.snapshot(),
                                  "pending_approval": approval if approval and approval["conversation_id"] == conversation_id else None,
                                  "observation": self._observations.get(conversation_id)})

    def call(self, method, data, *, timeout=10):
        request_id = data["client_request_id"]
        identity = hashlib.sha256(json.dumps([method, data], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._mutex:
            receipt = self._receipts.get(request_id)
            if receipt is not None:
                if receipt.fingerprint != identity:
                    raise ServiceError("同一请求 ID 不能用于不同操作。", code="request_id_conflict")
            else:
                runtime = self.snapshot()
                if not runtime["execution_connected"]:
                    raise ServiceError(runtime["message"] or "执行服务不可用，请重新启动 Web 入口。",
                                       code="worker_unavailable", status=503)
                if method == "approval":
                    approval = self._pending_approval
                    if self._approval_sending or not approval:
                        raise ServiceError("审批已处理或失效，请刷新当前任务。", code="stale_approval")
                    if (data.get("approval_id") != approval["id"]
                            or data.get("task_id") != approval["task_id"]
                            or data.get("conversation_id") != approval["conversation_id"]):
                        raise ServiceError("审批不属于当前等待的任务。", code="stale_approval")
                    self._approval_sending = True
                else:
                    if self._busy:
                        raise ServiceError("已有任务正在执行或等待审批，请先处理当前任务。", code="worker_busy")
                    self._busy = True
                    self._last_error = None
                    self._active = {"conversation_id": data.get("conversation_id"),
                                    "task_id": data.get("task_id"), "phase": "starting"}
                receipt = _Receipt(method, identity)
                self._version += 1
                self._receipts[request_id] = receipt
                while len(self._receipts) > 512:
                    completed = next((key for key, item in self._receipts.items() if item.event.is_set()), None)
                    if completed is None:
                        break
                    del self._receipts[completed]
                try:
                    self._connection.send({"id": request_id, "method": method, "data": data})
                except (OSError, EOFError):
                    self._busy = self._approval_sending = False
                    receipt.error = {"code": "worker_lost", "status": 503, "message": "后台连接已中断，请刷新任务检查点。"}
                    receipt.event.set()
        if not receipt.event.wait(timeout):
            raise ServiceError("请求尚未确认，请刷新任务状态；重试时必须沿用原请求 ID。",
                               code="acceptance_unknown", status=504)
        if receipt.error:
            raise ServiceError(receipt.error["message"], code=receipt.error["code"], status=receipt.error["status"])
        return receipt.result

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.pid is not None:
            if process.is_alive():
                try:
                    os.kill(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
        if self._connection is not None:
            self._connection.close()
        if self._reader is not None:
            self._reader.join(1)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
