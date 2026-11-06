"""独立 token 统计：请求前粗估输入，响应后读取服务端 usage。

不调用 API、不保存历史，不进行阈值判断或压缩。估算与服务端统计分别返回，
不累加成所谓“当前窗口占用”。字符估算不是精确分词，也不是 token 数的保证上界。
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def _nonnegative_integer(value: Any, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 必须是非负整数")


def _check_ratio(tokens_per_char: float) -> None:
    if (isinstance(tokens_per_char, bool) or not isinstance(tokens_per_char, (int, float))
            or not math.isfinite(tokens_per_char) or tokens_per_char <= 0):
        raise ValueError("tokens_per_char 必须是有限正数")


@dataclass(frozen=True)
class TokenEstimate:
    """估算输入的分项；不包含模型尚未生成的输出，也不是 API 实测用量。"""

    message_tokens: int
    tool_tokens: int
    overhead_tokens: int
    tokens_per_char: float

    @property
    def estimated_input_tokens(self) -> int:
        return self.message_tokens + self.tool_tokens + self.overhead_tokens


@dataclass(frozen=True)
class TokenUsage:
    """单次响应报告的用量；None 表示未提供，0 表示明确报告为零。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative_integer(value, name)


def estimate_text_tokens(text: str, *, tokens_per_char: float = 2.0) -> int:
    """按 Unicode 字符数乘系数并向上取整；不按 UTF-8 字节数计数。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    _check_ratio(tokens_per_char)
    return math.ceil(len(text) * tokens_per_char)


def _json_text(items: Sequence[Mapping[str, Any]], name: str) -> str:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise TypeError(f"{name} 必须是字典列表或元组")
    if not all(isinstance(item, Mapping) for item in items):
        raise TypeError(f"{name} 中的每项必须是字典")
    return json.dumps([dict(item) for item in items], ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def estimate_request_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    tokens_per_char: float = 2.0,
    extra_overhead_tokens: int = 0,
) -> TokenEstimate:
    """估算待发送的当前分支及工具 schema，不接收整棵 History 或整个请求体。

    messages 应使用 message_to_dict 转换后的 API 字典格式；tools 是 API
    工具定义列表。整个消息结构均计入，包括系统内容、工具代码、参数、结果和关联 ID。
    不额外统计 model、认证信息、max_tokens 等非上下文配置。

    JSON 字符数只是近似表示，序列化标点和转义也计入；并不等于服务端实际分词。
    序列化结构已计入估算。服务端实际协议开销未知，不编造固定的逐消息开销；
    调用方如掌握额外开销，可显式传入 extra_overhead_tokens，默认不另加。
    """
    _check_ratio(tokens_per_char)
    _nonnegative_integer(extra_overhead_tokens, "extra_overhead_tokens")
    message_text = _json_text(messages, "messages")
    tool_text = _json_text(tools, "tools") if tools is not None else ""
    return TokenEstimate(
        message_tokens=estimate_text_tokens(message_text, tokens_per_char=tokens_per_char),
        tool_tokens=estimate_text_tokens(tool_text, tokens_per_char=tokens_per_char) if tools else 0,
        overhead_tokens=extra_overhead_tokens,
        tokens_per_char=tokens_per_char,
    )


def parse_usage(response: Mapping[str, Any]) -> TokenUsage | None:
    """读取完整 API 响应中的 usage；缺失、null 或没有可识别计数时返回 None。

    部分统计可以保留，不根据已有字段猜测缺失值；不将缓存/推理子项再加到总数中。
    字段类型错误、布尔值或负数会抛 ValueError，由将来的接入层决定如何降级。
    不检查服务商报告的 total 是否等于两项之和，也不跨请求累计。
    """
    if not isinstance(response, Mapping):
        raise TypeError("response 必须是 API 响应字典")
    raw = response.get("usage")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("usage 必须是对象或 null")
    usage = TokenUsage(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
    )
    if all(value is None for value in (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)):
        return None
    return usage
