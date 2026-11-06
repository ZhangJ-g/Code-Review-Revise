"""模型、执行器和历史节点之间的同步循环。"""

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from .history import History
from .context_manager import ContextManager, PreparedContext, RequestReport, prepare_messages
from .model_api import ErrorKind, ModelError, ModelReply, RetryPolicy
from .memory_store import JsonMemoryStore
from .message_utils import pending_tool_calls
from .models import Message, Role, Session, TaskRun, ToolCall, new_id
from .prompts import build_system_message
from .runner import RunResult, run_code
from .tools import ToolRegistry


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
        context_manager: ContextManager | None = None,
        retry_policy: RetryPolicy | None = None,
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
        self._task: TaskRun | None = None
        self._checkpoint_failed = False
        self.context_manager = context_manager
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self.request_reports: list[RequestReport] = []
        self.last_round_context: PreparedContext | None = None
        self.last_round_error: str | None = None

    @property
    def history(self) -> list[Session]:
        """兼容原先的读取方式；这里仅包含当前分支，不包含其他分支。"""
        return self.history_tree.path() if self.history_tree is not None else []

    @staticmethod
    def _pending_calls(entries: list[Session]) -> list[ToolCall]:
        """检查消息关联，按声明顺序返回尚未得到结果的调用。"""
        return pending_tool_calls([entry.message for entry in entries])

    @staticmethod
    def _check_resumable(entries: list[Session]) -> None:
        """普通新任务要求路径完整；原任务的部分执行由 resume 单独处理。"""
        if Agent._pending_calls(entries):
            raise ValueError("历史停在未完成的工具调用，拒绝自动重放；请先核对实际副作用，或从更早的完整节点创建分支")

    @staticmethod
    def validate_resume(history: History, *, parent_id: str | None = None) -> None:
        """供入口在选择分支时检查；不改叶节点，不执行工具。"""
        if history.unfinished_task(parent_id) is not None:
            raise ValueError("此位置有未结束的任务，请恢复原任务，或从更早的完整节点创建分支")
        Agent._check_resumable(history.path(parent_id))

    @staticmethod
    def validate_recovery(history: History, task_id: str) -> TaskRun:
        history.validate()
        task = history.tasks.get(task_id)
        if task is None or task.status in {"completed", "cancelled"}:
            raise ValueError("任务不存在或已经结束")
        if history.nodes[task.checkpoint_node_id].children_ids:
            raise ValueError("任务检查点已有后续分支，不能在它后面重放原任务")
        entries = history.path(task.checkpoint_node_id)
        pending = Agent._pending_calls(entries)
        if task.phase == "waiting_model" and pending:
            raise ValueError("模型等待阶段却存在未完成工具调用，检查点不一致")
        if task.phase == "waiting_approval" and (
            not pending or pending[0].id != task.active_tool_call_id
        ):
            raise ValueError("待确认工具与检查点不一致")
        if task.phase == "executing_tool":
            latest_batch = next((entry.message.tool_calls for entry in reversed(entries)
                                 if entry.message.tool_calls), [])
            ids = [call.id for call in latest_batch]
            if task.active_tool_call_id not in ids:
                raise ValueError("执行标记不属于最后一批工具调用")
            pending_ids = [call.id for call in pending]
            if task.active_tool_call_id in pending_ids:
                valid = pending_ids[0] == task.active_tool_call_id
            elif pending_ids:
                valid = ids.index(pending_ids[0]) == ids.index(task.active_tool_call_id) + 1
            else:
                valid = ids[-1] == task.active_tool_call_id
            if not valid:
                raise ValueError("工具执行标记与已有结果的顺序不一致")
        return task

    def _activate(self, history: History, task: TaskRun) -> None:
        """
        接管指定历史树和任务，作为后续执行对象。
        保存失败后，拒绝继续使用原内存历史，要求重新加载。
        恢复任务时，将当前分支对准该任务的检查点，
        确保后续模型请求使用正确的历史路径。
        """
        if self._checkpoint_failed and history is self.history_tree:
            raise ValueError("上次保存失败，请从磁盘重新加载历史后操作")
        self.history_tree = history
        self._task = task
        self._checkpoint_failed = False
        self.request_reports = []
        self.last_round_context = None
        self.last_round_error = None
        if history.conversation.current_leaf_id != task.checkpoint_node_id:
            history.select_leaf(task.checkpoint_node_id)# 如果不同 切换到任务检查点

    def _checkpoint(
        self, *, phase: str, status: str = "running",
        active_tool_call_id: str | None = None,
        messages: tuple[Message, ...] = (), error: str | None = None,
    ) -> None:
        history, task = self.history_tree, self._task
        if history is None or task is None:
            raise RuntimeError("没有活动任务")
        try:
            for message in messages:
                history.append(message)
            now = datetime.now(timezone.utc)
            task.checkpoint_node_id = history.conversation.current_leaf_id
            task.phase, task.status = phase, status
            task.active_tool_call_id, task.error = active_tool_call_id, error
            task.updated_at = history.conversation.updated_at = now
            task.finished_at = now if status in {"completed", "cancelled"} else None
            # 消息和任务状态在同一份快照中提交，不拆成两个文件或两次保存。
            self.store.save(history)
        except BaseException:
            # 不能在保存失败后再把内存中的未提交状态当作检查点补写。
            self._checkpoint_failed = True
            raise

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
        if self._checkpoint_failed and history is self.history_tree:
            raise ValueError("上次保存失败，请重新加载历史")
        if history is None:#system_prompt as root
            if parent_id is not None:
                raise ValueError("创建分支需要传入已有 history")
            history = History.create(build_system_message(self.registry))
        self.validate_resume(history, parent_id=parent_id)
        target_id = history.conversation.current_leaf_id if parent_id is None else parent_id
        if history.unfinished_task(target_id) is not None:
            raise ValueError("此位置有未结束的任务，请先恢复该任务，或从更早节点创建分支")
        user = history.append(Message(Role.USER, content=task), parent_id=parent_id)
        execution = TaskRun(user_node_id=user.id, checkpoint_node_id=user.id)
        # history.tasks[execution.id] = execution 只是加入内存中的字典，并没有直接写入 JSON。
        history.tasks[execution.id] = execution
        """
        把刚刚创建的
        TaskRun
        放进任务字典。

        目的：
        - 保存任务执行记录。
        - 后续可以通过任务
        ID
        找到它。
        - 记录任务状态、执行阶段和检查点。
        - 为后续恢复提供依据。
        """
        self._activate(history, execution)#前面放到字典是为了记录当前执行的任务，然后进行比较是否是当前节点，优先恢复之前没做完的任务
        self._checkpoint(phase="waiting_model")
        return self._execute_task()

    def resume(self, history: History, task_id: str) -> AgentResult:
        """继续原任务，不重新追加用户 task，不重跑已有结果的工具。"""
        task = self.validate_recovery(history, task_id)
        self._activate(history, task)
        if task.phase == "executing_tool" or task.status == "needs_review":
            self._checkpoint(
                phase=task.phase, status="needs_review", active_tool_call_id=task.active_tool_call_id,
                error="上次工具结果不确定，需确认旧进程停止并核对实际副作用",
            )
            return AgentResult("needs_review", task.error, task.rounds)
        self._checkpoint(phase=task.phase, active_tool_call_id=task.active_tool_call_id)
        return self._execute_task()

    def acknowledge_unknown(
        self, history: History, task_id: str, note: str, *, confirmed_stopped: bool = False,
    ) -> None:
        """只记录人工核对并关闭不确定调用，不重新执行任何旧代码。"""
        if not confirmed_stopped or not note.strip():
            raise ValueError("请先确认旧进程和工具子进程已停止，并填写实际核对情况")
        task = self.validate_recovery(history, task_id)
        if task.phase != "executing_tool" and task.status != "needs_review":
            raise ValueError("此任务不处于结果未知阶段")
        self._activate(history, task)
        messages = []
        for call in self._pending_calls(self.history):
            unknown = call.id == task.active_tool_call_id
            code = "TOOL_OUTCOME_UNKNOWN" if unknown else "TOOL_NOT_STARTED"
            result = RunResult(
                "unknown" if unknown else "not_started",
                error=f"{code}：中断恢复生成的状态说明，不是成功结果；禁止盲目重放原代码",
            )
            messages.append(self._tool_message(call, result))
        messages.append(Message(
            Role.USER,
            "原任务恢复核对（不是新任务）：我已确认旧程序及工具子进程已停止。"
            f"实际核对情况：{note.strip()}。请基于当前状态继续分析，先验证副作用，不要直接重复未知结果的代码。",
        ))
        self._checkpoint(phase="waiting_model", status="interrupted", messages=tuple(messages))

    def cancel_task(self, history: History, task_id: str) -> AgentResult:
        """明确结束未完成任务；未知副作用必须先经人工核对。"""
        task = self.validate_recovery(history, task_id)
        if task.phase == "executing_tool" or task.status == "needs_review":
            raise ValueError("工具结果不确定，请先核对进程及实际副作用")
        self._activate(history, task)
        messages = tuple(self._tool_message(call, RunResult("cancelled", error="用户结束原任务，此调用未执行"))
                         for call in self._pending_calls(self.history))
        self._checkpoint(phase="finished", status="cancelled", messages=messages, error="用户明确结束原任务")
        return AgentResult("cancelled", "原任务已结束，不再继续执行；已有修改保留。", task.rounds)

    @staticmethod
    def _tool_message(call: ToolCall, result: RunResult) -> Message:
        return Message(Role.TOOL, json.dumps(asdict(result), ensure_ascii=False), tool_call_id=call.id)

    def _execute_pending(self) -> AgentResult | None:
        """
        完成未完成的任务 任何未完成状态的任务 都可以传进来执行，实现函数的复用
        """
        calls = self._pending_calls(self.history)
        for index, call in enumerate(calls):
            self._checkpoint(phase="waiting_approval", active_tool_call_id=call.id)
            if call.name != "run_code":
                result = RunResult("error", error="模型只能通过 run_code 调用工具")
            elif (set(call.arguments) != {"code"} or not isinstance(call.arguments["code"], str)
                  or not call.arguments["code"].strip()):
                result = RunResult("error", error="run_code 需要且只接受非空字符串参数 code")
            else:
                code = call.arguments["code"]
                if not self.approve(code):
                    # 一次提交所有未执行调用的取消结果，恢复时不再询问它们。
                    results = tuple(self._tool_message(item, RunResult("cancelled", error="用户取消，未执行"))
                                    for item in calls[index:])
                    self._checkpoint(phase="finished", status="cancelled", messages=results)
                    return AgentResult("cancelled", "任务已停止；此前执行过的修改不会回滚。", self._task.rounds)
                self._checkpoint(phase="executing_tool", active_tool_call_id=call.id)
                """
                ***这里完成任务的恢复执行！！返回结果
                """
                result = run_code(code, self.registry, approved=True, timeout=self.code_timeout)
            if result.status in {"timeout", "unknown"}:
                self._checkpoint(
                    phase="executing_tool", status="needs_review", active_tool_call_id=call.id,
                    messages=(self._tool_message(call, result),), error=result.error,
                )
                return AgentResult("needs_review", "未取得可靠的工具结果；请确认旧进程已停止并核对文件状态后恢复。", self._task.rounds)
            next_call = calls[index + 1] if index + 1 < len(calls) else None
            self._checkpoint(
                phase="waiting_approval" if next_call else "waiting_model",
                active_tool_call_id=next_call.id if next_call else None,
                messages=(self._tool_message(call, result),),
            )
        return None

    def _prepare_context(
        self, *, force_compact: bool = False, previous_fingerprint: str | None = None,
    ) -> PreparedContext:
        #为当前这次大模型请求准备上下文，
        if self.context_manager is None:
            # 保留底层 Agent 的旧用法；main 始终注入带窗口预算的管理器。
            return prepare_messages([entry.message for entry in self.history],
                                    getattr(self.client, "tool_definitions", []),
                                    max_output_tokens=getattr(self.client, "max_output_tokens", None))
        try:
            return self.context_manager.prepare(self.history_tree, self._task.id, force_compact=force_compact,
                                                previous_fingerprint=previous_fingerprint)
        except OSError:
            self._checkpoint_failed = True
            raise  # 摘要保存失败同样要求重新加载，不能补写未提交内存。

    def _validate_reply(self, reply: Message) -> None:
        try:
            if not isinstance(reply, Message) or not isinstance(reply.content, str):
                raise ValueError("invalid message")
            if reply.role != Role.ASSISTANT:
                raise ValueError("unexpected role")
            if not reply.tool_calls and not reply.content.strip():
                raise ValueError("empty reply")
            # 在落盘或执行之前检查新调用的 ID 与历史是否冲突。
            self._pending_calls(self.history + [Session(message=reply)])
        except (ValueError, TypeError, AttributeError):
            raise ModelError("助手响应为空、格式无效或工具调用 ID 冲突；未写入历史或执行工具",
                             kind=ErrorKind.RESPONSE_INVALID) from None

    def _request_reply(self) -> Message:
        # 预检/摘要有自己的错误边界，不能被当作主请求错误再次整轮重试。
        prepared = self._prepare_context()
        context_recovered = False
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            self._task.rounds += 1  # 记录实际请求尝试；一次决策可能有多次请求。
            self._checkpoint(phase="waiting_model")
            # 为本次实际请求创建观测记录，记录上下文指纹、摘要 ID 和 Token 估算。
            report = RequestReport(new_id(), prepared.fingerprint, prepared.compaction_id,
                                   prepared.estimate.estimated_input_tokens, prepared.input_budget)
            self.request_reports.append(report)
            try:
                # 优先使用支持返回 Token usage 等信息的接口，否则使用普通请求接口。
                complete_with_usage = getattr(self.client, "complete_with_usage", None)#从 self.client 对象取出 complete_with_usage 方法；没有这个方法则返回 None。
                if complete_with_usage is not None:
                    cap = self.context_manager.config.max_output_tokens if self.context_manager else None
                    result = complete_with_usage(prepared.messages, max_output_tokens=cap)#取出消息 发送最大token限制
                else:
                    result = ModelReply(self.client.complete(prepared.messages))
                if not isinstance(result, ModelReply):
                    raise ModelError("模型客户端必须返回 ModelReply", kind=ErrorKind.RESPONSE_INVALID)
                report.usage, report.usage_error = result.usage, result.usage_error
                report.response_id = result.response_id
                self._validate_reply(result.message)
                return result.message
            except ModelError as error:
                report.error_kind = error.kind

                # 2. 上下文超限：不能简单重发原请求，需要先压缩并重新构建上下文。
                if error.kind == ErrorKind.CONTEXT_LIMIT:
                    if (self.context_manager is None or context_recovered
                            or attempt >= self.retry_policy.max_attempts):
                        raise
                    context_recovered = True

                    # 强制触发压缩，并通过 previous_fingerprint 防止上下文没有变化时重复发送。
                    # 压缩完成后重新构建 PreparedContext，再进入下一轮模型请求。
                    prepared = self._prepare_context(force_compact=True, previous_fingerprint=prepared.fingerprint)
                    continue
                delay = self.retry_policy.delay(attempt, error)
                if delay is None:
                    raise
                #  # 3. 其他可重试错误：例如网络异常、临时服务错误。
                #    不改变上下文，只等待一段时间后使用原请求内容重试。
                time.sleep(delay)

    def _after_round(self) -> None:
        """本轮结束后，基于最新历史构造上下文视图并估算 Token。

        inspect() 会检查已有摘要的适用路径和安全边界，选择可用摘要，
        组装“系统消息 + 已有摘要 + 边界后的原始消息”；没有摘要则使用原始路径。
        它不会调用模型生成新摘要，不新增压缩记录，也不修改任务检查点。

        last_round_context 保存轮末观测快照，供统计展示使用，不直接发送给模型。
        下一次请求由 _request_reply() 调用 _prepare_context()，重新构造上下文；
        ContextManager.prepare() 在超预算或强制压缩时调用 compact()，
        尝试为安全旧历史生成并保存新摘要，再重新组装和检查输入预算。
        系统消息、当前任务及其他受保护记录不会因此被压缩。
        """
        self.last_round_context = None
        self.last_round_error = None
        try:
            if self.context_manager is None:
                # 兼容未注入管理器的用法：只组装原始分支消息并估算，不会生成摘要。
                self.last_round_context = self._prepare_context()
            else:
                self.last_round_context = self.context_manager.inspect(self.history_tree, self._task.id)
        except Exception as error:
            self.last_round_error = f"本轮上下文统计失败（{type(error).__name__}），任务执行结果已保留"

    def _loop(self) -> AgentResult:
        if self._pending_calls(self.history):#寻找未完成，_pending_calls
            result = self._execute_pending()#执行未完成
            self._after_round()
            if result is not None:
                return result
        for _ in range(self.max_rounds):
            reply = self._request_reply()
            if not reply.tool_calls:
                self._checkpoint(phase="finished", status="completed", messages=(reply,))
                self._after_round()#这里的功能就拿到了上一轮的上下文
                return AgentResult("completed", reply.content, self._task.rounds)
            self._checkpoint(#传进去直接更新检查点
                phase="waiting_approval", active_tool_call_id=reply.tool_calls[0].id, messages=(reply,),
            )
            result = self._execute_pending()#带检查点的都是没有执行完的交给他继续执行完
            self._after_round()
            if result is not None:
                return result
        self._checkpoint(phase="waiting_model", status="limit", error="达到本次模型轮数上限")#传进去直接更新检查点
        return AgentResult("limit", "达到本次轮数上限，任务已保存，可选择恢复。", self._task.rounds)

    def _execute_task(self) -> AgentResult:
        try:
            return self._loop()
        except KeyboardInterrupt:
            if not self._checkpoint_failed and self._task.status not in {"completed", "cancelled"}:
                task = self._task
                try:
                    self._checkpoint(
                        phase=task.phase,
                        status="needs_review" if task.phase == "executing_tool" else "interrupted",
                        active_tool_call_id=task.active_tool_call_id, error="用户中断",
                    )
                except OSError:
                    pass  # 不能保存时保留上一个磁盘检查点，由下次加载判断。
            raise
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            if self._checkpoint_failed:
                raise
            task = self._task
            if task.status in {"completed", "cancelled"}:
                raise  # 已提交的完成状态不能被后续观测错误改成 interrupted。
            status = "needs_review" if task.phase == "executing_tool" else "interrupted"
            self._checkpoint(phase=task.phase, status=status, active_tool_call_id=task.active_tool_call_id, error=str(error))
            self._after_round()
            if isinstance(error, ModelError) and error.kind == ErrorKind.CONFIGURATION:
                raise  # 配置错误保留检查点后，交给入口停止并报告。
            return AgentResult("needs_review" if status == "needs_review" else "error", str(error), task.rounds)
