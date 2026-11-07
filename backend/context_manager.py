"""上下文窗口模块：解决“这次给模型发送什么，以及输入是否装得下”。

调用关系：Agent → ContextManager(发现问题) → CompactionManager(解决问题)->ContextManager(验证问题、重新组装上下文)。
本模块统计主请求的 messages 和工具定义，按总窗口、输出预留和安全余量判断预算；
需要压缩时委托 CompactionManager，再重新组装和估算。压缩细节由后者负责。
不依赖 Agent 或具体模型客户端，不直接发送主请求或执行业务工具。
"""

import hashlib
import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from .compaction_manager import CompactionError
from .history import History
from .message_utils import message_to_dict
from .model_api import ErrorKind, ModelError
from .models import Compaction, MemorySnapshot, Message
from .persistent_memory_management import MemoryProvider
from .prompts import with_long_term_memory
from .token_stats import TokenEstimate, TokenUsage, estimate_request_tokens, estimate_text_tokens


def _token_count_from_env(name: str, default: str | None = None) -> int:
    """token 数量允许整数及 K/M 单位；不猜测具体模型的规格。"""
    value = os.environ.get(name, default)
    if value is None or not value.strip():
        raise ValueError(f"请在环境或 .env 配置 {name}，填写整数 token 数或 K/M 单位，例如 272000 或 272K")
    text = value.strip()
    scale = {"k": 1000, "m": 1_000_000}.get(text[-1:].lower(), 1)
    if scale != 1:
        text = text[:-1].strip()
    try:
        return int(text) * scale
    except ValueError:
        raise ValueError(f"{name} 格式无效：请填写整数 token 数或 K/M 单位，例如 272000、272K、1M") from None


def _float_from_env(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} 格式无效：请填写数字；比例使用 0.2，不使用 20%") from None


@dataclass(frozen=True)
class ContextConfig:
    context_window: int
    max_output_tokens: int
    reserve_ratio: float = 0.2
    safety_tokens: int = 512
    tokens_per_char: float = 2.0
    summary_max_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("MODEL_CONTEXT_WINDOW", self.context_window),
            ("MODEL_MAX_OUTPUT_TOKENS", self.max_output_tokens),
            ("MODEL_SUMMARY_MAX_TOKENS", self.summary_output_limit),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数 token 数")
        if type(self.safety_tokens) is not int or self.safety_tokens < 0:
            raise ValueError("MODEL_TOKEN_SAFETY_MARGIN 必须是非负整数")
        if not math.isfinite(self.reserve_ratio) or not 0 <= self.reserve_ratio < 1:
            raise ValueError("MODEL_CONTEXT_RESERVE_RATIO 必须在 [0, 1) 范围")
        try:
            estimate_text_tokens("", tokens_per_char=self.tokens_per_char)
        except (ValueError, TypeError):
            raise ValueError("MODEL_TOKENS_PER_CHAR 必须是有限正数，例如 2") from None
        for label, budget, output_name in (
            ("主请求", self.input_budget, "MODEL_MAX_OUTPUT_TOKENS"),
            ("摘要请求", self.summary_input_budget, "MODEL_SUMMARY_MAX_TOKENS"),
        ):
            if budget <= 0:
                raise ValueError(
                    f"{label}输入预算为 {budget}：输出及安全预留已占满 MODEL_CONTEXT_WINDOW；"
                    f"请检查 {output_name}、MODEL_TOKEN_SAFETY_MARGIN 和 MODEL_CONTEXT_RESERVE_RATIO"
                )

    @property
    def summary_output_limit(self) -> int:
        return min(1024, self.max_output_tokens) if self.summary_max_tokens is None else self.summary_max_tokens

    def budget_for(self, output_tokens: int) -> int:
        reserve = max(math.ceil(self.context_window * self.reserve_ratio), output_tokens + self.safety_tokens)
        return self.context_window - reserve

    @property
    def input_budget(self) -> int:
        return self.budget_for(self.max_output_tokens)

    @property
    def summary_input_budget(self) -> int:
        return self.budget_for(self.summary_output_limit)

    @classmethod
    def from_env(cls) -> "ContextConfig":
        """只读进程环境；main 先由 ModelConfig 加载项目 .env。"""
        summary = os.environ.get("MODEL_SUMMARY_MAX_TOKENS", "").strip()
        return cls(
            context_window=_token_count_from_env("MODEL_CONTEXT_WINDOW"),
            max_output_tokens=_token_count_from_env("MODEL_MAX_OUTPUT_TOKENS"),
            reserve_ratio=_float_from_env("MODEL_CONTEXT_RESERVE_RATIO", "0.2"),
            safety_tokens=_token_count_from_env("MODEL_TOKEN_SAFETY_MARGIN", "512"),
            tokens_per_char=_float_from_env("MODEL_TOKENS_PER_CHAR", "2"),
            summary_max_tokens=_token_count_from_env("MODEL_SUMMARY_MAX_TOKENS") if summary else None,
        )


class ContextCompactor(Protocol):
    """这三个方法只是声明必须具备的接口，并没有创建任何具体对象，也没有实现压缩功能。"""
    def select(self, history: History, current_task_id: str) -> Compaction | None: ...
    def build_context(self, history: History, current_task_id: str) -> list[Message]: ...
    def compact(self, history: History, current_task_id: str) -> Compaction: ...


@dataclass(frozen=True)
class PreparedContext:
    messages: list[Message]
    estimate: TokenEstimate
    input_budget: int | None
    compaction_id: str | None
    fingerprint: str
    # 记录这份上下文使用的记忆版本和 ID；不作为额外字段发给模型。
    memory_revision: int = 0
    memory_id: str | None = None


@dataclass
class RequestReport:
    """当前调用期间的观测记录，不是对话消息，也不写入历史树。"""

    request_id: str
    context_fingerprint: str
    compaction_id: str | None
    estimated_input_tokens: int
    input_budget: int | None
    usage: TokenUsage | None = None
    usage_error: str | None = None
    response_id: str | None = None
    error_kind: ErrorKind | None = None
    # 方便排查：这次实际请求用了哪个版本、哪条记忆。
    memory_revision: int = 0
    memory_id: str | None = None


def prepare_messages(
    messages: list[Message], tools: list[dict], *, tokens_per_char: float = 2.0,
    input_budget: int | None = None, compaction_id: str | None = None,
    max_output_tokens: int | None = None,
    memory_snapshot: MemorySnapshot | None = None,
) -> PreparedContext:
    """估算最终消息的 Token，并计算指纹（用来判断请求内容是否变化）。"""
    wire = [message_to_dict(message) for message in messages]
    estimate = estimate_request_tokens(wire, tools, tokens_per_char=tokens_per_char)
    serialized = json.dumps({"messages": wire, "tools": tools, "max_output_tokens": max_output_tokens},
                            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    snapshot = memory_snapshot or MemorySnapshot()
    return PreparedContext(deepcopy(messages), estimate, input_budget, compaction_id,
                           hashlib.sha256(serialized.encode("utf-8")).hexdigest(), snapshot.revision,
                           snapshot.memory.id if snapshot.memory else None)


class ContextManager:
    """管理“何时需要压缩，以及最终返回哪份模型可见上下文”。

    发送前调用 prepare()：
    1. 委托压缩管理器选择适用摘要并构造消息视图，计入工具定义估算输入。
    2. 与配置的主请求输入预算比较；超预算或明确要求恢复上下文超限时，
       委托 compact() 压缩安全旧历史。
    3. 压缩后重新构造、重新估算，确认输入实际缩小且符合预算，
       返回 PreparedContext，由 Agent 使用其中的 messages 请求模型。
       没有安全压缩空间或仍然过大时报告错误，不压缩当前任务来强行发送。

    每轮结束调用 inspect()：只构造当前视图并统计，不生成摘要或写入历史。
    例如主请求预算为 8000、输入估算为 9000 时，本类决定触发压缩；
    CompactionManager 决定压缩哪些历史。本类再检查压缩后是否低于预算。
    ！！！
    本类检查整个主请求的输入；
    CompactionManager 检查生成摘要那次请求的预算。
    摘要安全边界、累计摘要及保存由后者负责，模型请求重试与工具执行由 Agent 负责。
    """

    def __init__(self, config: ContextConfig, compactor: ContextCompactor, tools: list[dict],
                 memory_manager: MemoryProvider | None = None) -> None:
        """
        Protocol 不是让 ContextManager 去调用 Protocol 里面的函数，而是把 ContextManager 对外部依赖的最低能力要求描述出来。
        compactor: ContextCompactor这里声明是为了表达出最少都需要当前函数的接口、能力、具体什么能力不重要
        """
        self.config = config
        self.compactor = compactor
        self.tools = deepcopy(tools)
        self.memory_manager = memory_manager

    def get_memory_snapshot(self) -> MemorySnapshot:
        """取当前记忆状态；读取对象可能是本地管理器，也可能是 Web 进程代理。"""
        return self.memory_manager.get_snapshot() if self.memory_manager is not None else MemorySnapshot()

    def inspect(self, history: History, task_id: str, *, memory_snapshot: MemorySnapshot | None = None) -> PreparedContext:
        """选已有摘要、组装历史、追加当前记忆，再统计 Token。

        不生成新摘要，也不保存历史，轮末统计和页面预览都可以调用。
        """
        record = self.compactor.select(history, task_id)#选择压缩的摘要
        snapshot = memory_snapshot if memory_snapshot is not None else self.get_memory_snapshot()
        # 压缩器只管历史；历史组好后再加记忆，二者一起计入 Token 和指纹。
        messages = with_long_term_memory(self.compactor.build_context(history, task_id), snapshot.memory)
        return prepare_messages(
            messages, self.tools,
            tokens_per_char=self.config.tokens_per_char, input_budget=self.config.input_budget,
            compaction_id=record.id if record else None, max_output_tokens=self.config.max_output_tokens,
            memory_snapshot=snapshot,
        )

    def prepare(#对上下文是否满足做判断
        self, history: History, task_id: str, *, force_compact: bool = False,
        previous_fingerprint: str | None = None,
        memory_snapshot: MemorySnapshot | None = None,
    ) -> PreparedContext:
        # 一次预检（发送前检查）始终用同一份记忆。
        # 这样比较压缩前后 Token 时，不会因为用户换了记忆而算错压缩效果。
        snapshot = memory_snapshot if memory_snapshot is not None else self.get_memory_snapshot()
        #!!这里获取上下文视图
        prepared = self.inspect(history, task_id, memory_snapshot=snapshot)
        if force_compact or prepared.estimate.estimated_input_tokens > self.config.input_budget:
            try:
                self.compactor.compact(history, task_id)
            except CompactionError as error:
                raise ModelError(f"无法安全压缩：{error}", kind=ErrorKind.CONTEXT_LIMIT) from None
            after = self.inspect(history, task_id, memory_snapshot=snapshot)
            if (after.fingerprint == prepared.fingerprint
                    or after.estimate.estimated_input_tokens >= prepared.estimate.estimated_input_tokens):
                raise ModelError("没有进一步缩小的安全空间；保留当前任务并暂停，请检查窗口配置或工具输出大小",
                                 kind=ErrorKind.CONTEXT_LIMIT)
            prepared = after
        if previous_fingerprint is not None and prepared.fingerprint == previous_fingerprint:
            raise ModelError("上下文没有变化，停止重复发送相同的超限请求", kind=ErrorKind.CONTEXT_LIMIT)
        if prepared.estimate.estimated_input_tokens > self.config.input_budget:
            raise ModelError(
                f"压缩后输入估算 {prepared.estimate.estimated_input_tokens} 仍超过预算 {self.config.input_budget}；"
                "当前任务不可压缩，已暂停", kind=ErrorKind.CONTEXT_LIMIT,
            )
        return prepared
