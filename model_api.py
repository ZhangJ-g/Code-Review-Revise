"""模型接口的公共结果和错误类型，不依赖 HTTP、Agent 或存储。"""

import math
from dataclasses import dataclass
from enum import Enum

from models import Message
from token_stats import TokenUsage


class ErrorKind(str, Enum):
    CONTEXT_LIMIT = "context_limit"
    RESPONSE_INVALID = "response_invalid"
    TRANSIENT = "transient"
    CONFIGURATION = "configuration"
    OUTPUT_LIMIT = "output_limit"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class ModelError(Exception):
    def __init__(
        self, message: str, *, kind: ErrorKind = ErrorKind.UNKNOWN,
        status: int | None = None, provider_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.kind = kind
        self.status = status
        self.provider_code = provider_code
        self.retry_after = retry_after
        super().__init__(message if kind == ErrorKind.UNKNOWN else f"[{kind.value}] {message}")


@dataclass(frozen=True)
class ModelReply:
    message: Message
    usage: TokenUsage | None = None
    usage_error: str | None = None
    response_id: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 5.0

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts <= 0:
            raise ValueError("最大请求尝试次数必须为正整数")
        if any(not math.isfinite(value) or value < 0 for value in (self.base_delay, self.max_delay)):
            raise ValueError("重试等待时间必须为有限非负数")

    def delay(self, failed_attempt: int, error: ModelError) -> float | None:
        if (failed_attempt >= self.max_attempts
                or error.kind not in {ErrorKind.RESPONSE_INVALID, ErrorKind.TRANSIENT}):
            return None
        if error.retry_after is not None and error.retry_after > self.max_delay:
            return None  # 不忽略服务端长时间等待要求，也不阻塞终端很久。
        delay = min(self.base_delay * 2 ** (failed_attempt - 1), self.max_delay)
        return max(delay, error.retry_after or 0.0)
