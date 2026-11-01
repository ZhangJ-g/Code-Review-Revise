"""模型、执行器和历史节点之间的同步循环。"""

import json
import math
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from history import History
from llm import ModelError
from memory_store import JsonMemoryStore
from models import Message, Role, Session, ToolCall
from prompts import build_system_message
from runner import RunResult, run_code
from tools import ToolRegistry


class ModelClient(Protocol):
    def complete(self, messages: list[Message]) -> Message: ...


@dataclass
class AgentResult:
    status: str
    answer: str
    rounds: int


class Agent:
    def __init__(
        self,
        client: ModelClient,
        registry: ToolRegistry,
        approve: Callable[[str], bool],
        *,
        max_rounds: int = 10,
        code_timeout: float = 5.0,
        store: JsonMemoryStore | None = None,
    ) -> None:
        if max_rounds <= 0 or not math.isfinite(code_timeout) or code_timeout <= 0:
            raise ValueError("最大轮数和代码超时必须大于零，超时必须有限")
        self.client = client
        self.registry = registry
        self.approve = approve
        self.max_rounds = max_rounds
        self.code_timeout = code_timeout
        self.store = store if store is not None else JsonMemoryStore()
        self.history_tree: History | None = None

    @property
    def history(self) -> list[Session]:
        """兼容原先的读取方式；这里仅包含当前分支，不包含其他分支。"""
        return self.history_tree.path() if self.history_tree is not None else []

    def _append(self, message: Message) -> None:
        if self.history_tree is None:
            raise RuntimeError("请先建立对话历史")
        self.history_tree.append(message)
        # 保存失败直接停止，不继续调用模型或执行工具。
        self.store.save(self.history_tree)

    @staticmethod
    def _check_resumable(entries: list[Session]) -> None:
        """只检查当前分支的消息序列能否继续，不执行或重放工具。"""
        if not entries or entries[0].message.role != Role.SYSTEM:
            raise ValueError("Agent 历史必须以系统消息为根节点")
        pending = set()  # 已发起、尚未找到对应工具结果的调用 ID。
        seen = set()  # 当前分支中已出现过的全部调用 ID，用于检查重复。
        for entry in entries[1:]:
            message = entry.message
            if message.role == Role.SYSTEM:
                raise ValueError("系统消息只能位于历史根节点")
            if pending:
                if message.role != Role.TOOL or message.tool_call_id not in pending:
                    raise ValueError("历史工具调用与结果不匹配，不能继续对话")
                # 找到结果后只从 pending 移除；seen 保留该 ID，防止后续重复使用。
                pending.remove(message.tool_call_id)
            elif message.role == Role.TOOL:
                raise ValueError("历史存在未关联工具调用的工具结果")
            elif message.tool_calls:
                ids = [call.id for call in message.tool_calls]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    raise ValueError("当前分支存在重复工具调用 ID")
                pending = set(ids)
                seen.update(ids)
        if pending:
            raise ValueError("历史停在未完成的工具调用，拒绝自动重放；请先核对实际副作用，或从更早的完整节点创建分支")

    def _feedback(self, call: ToolCall, result: RunResult) -> None:
        self._append(Message(
            Role.TOOL,
            content=json.dumps(asdict(result), ensure_ascii=False),
            tool_call_id=call.id,
        ))

    @staticmethod
    def validate_resume(history: History, *, parent_id: str | None = None) -> None:
        """供入口在选择分支时检查；不改叶节点，不执行工具。"""
        Agent._check_resumable(history.path(parent_id))

    def run(
        self,
        task: str,
        *,
        history: History | None = None,
        parent_id: str | None = None,
    ) -> AgentResult:
        """默认新建对话；传入 history 继续，指定 parent_id 从旧节点分支。

        不自动选择最近文件，不自动重放中断的工具调用。
        """
        if not task.strip():
            raise ValueError("任务不能为空")
        if history is None:#system_prompt as root
            if parent_id is not None:
                raise ValueError("创建分支需要传入已有 history")
            history = History.create(build_system_message(self.registry))
        self.validate_resume(history, parent_id=parent_id)
        self.history_tree = history
        history.append(Message(Role.USER, content=task), parent_id=parent_id)
        self.store.save(history)
        for round_number in range(1, self.max_rounds + 1):
            try:
                #complete 完全、完成这里组装完成后 就提交了
                reply = self.client.complete([entry.message for entry in self.history])
            except ModelError as error:
                return AgentResult("error", str(error), round_number)
            self._append(reply)
            if not reply.tool_calls:
                return AgentResult("completed", reply.content, round_number)

            cancelled = False
            for call in reply.tool_calls:
                if cancelled:
                    result = RunResult("cancelled", error="用户已取消本轮后续执行")
                elif call.name != "run_code":
                    result = RunResult("error", error="模型只能通过 run_code 调用工具")
                elif (set(call.arguments) != {"code"}
                      or not isinstance(call.arguments["code"], str)
                      or not call.arguments["code"].strip()):
                    result = RunResult("error", error="run_code 需要且只接受非空字符串参数 code")
                else:
                    code = call.arguments["code"]
                    if not self.approve(code):
                        cancelled = True
                        result = RunResult("cancelled", error="用户未批准此代码，未执行")
                    else:
                        result = run_code(
                            code, self.registry, approved=True, timeout=self.code_timeout,
                        )
                self._feedback(call, result)
            if cancelled:
                return AgentResult("cancelled", "任务已停止；此前执行过的修改不会回滚。", round_number)

        return AgentResult("limit", "已达到最大模型轮数，任务尚未确认完成；请检查执行记录和文件状态。", self.max_rounds)
