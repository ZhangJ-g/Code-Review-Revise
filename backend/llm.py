"""可配置的 Chat Completions 兼容接口，使用 python-dotenv 加载本地配置。"""

import math
import json
import os
import ssl
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, InvalidURL
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .models import Message, Role, ToolCall
from .message_utils import message_to_dict
from .model_api import ErrorKind, ModelError, ModelReply, RetryPolicy
from .paths import ENV_FILE
from .token_stats import parse_usage
from dotenv import load_dotenv


RUN_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": "执行 Python 工具编排代码；代码会先交由用户确认，返回输出与异常。",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "完整的 Python 代码"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}


_CONTEXT_CODES = {"context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded"}
_CONFIG_CODES = {
    "invalid_api_key", "authentication_error", "permission_denied", "model_not_found",
    "invalid_request_error", "unsupported_parameter", "insufficient_quota",
    "billing_hard_limit_reached", "quota_exceeded",
}
_TRANSIENT_CODES = {"server_error", "rate_limit_exceeded", "overloaded_error", "service_unavailable"}


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _api_error(payload: object, status: int | None, retry_after: float | None = None) -> ModelError:
    """只在内存里检查错误内容；对外仅暴露分类、状态及已知代码。"""
    raw = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    code = raw.get("code") or raw.get("type")
    code = code.casefold() if isinstance(code, str) else ""
    message = raw.get("message", "")
    message = message.casefold() if isinstance(message, str) else ""
    context = code in _CONTEXT_CODES or any(phrase in message for phrase in (
        "maximum context length", "context length exceeded", "context window exceeded",
        "exceeds the context window", "上下文长度超过", "上下文长度超出",
    ))
    quota = code in {"insufficient_quota", "billing_hard_limit_reached", "quota_exceeded"} or any(
        phrase in message for phrase in ("insufficient quota", "exceeded your current quota", "billing hard limit")
    )
    if status in {401, 402, 403, 404, 405, 415} or quota:
        kind = ErrorKind.CONFIGURATION
    elif context:
        kind = ErrorKind.CONTEXT_LIMIT
    elif code in _CONFIG_CODES:
        kind = ErrorKind.CONFIGURATION
    elif code in _TRANSIENT_CODES or status in {408, 409, 425, 429} or (status is not None and 500 <= status < 600):
        kind = ErrorKind.TRANSIENT
    elif status in {400, 422}:
        kind = ErrorKind.CONFIGURATION
    else:
        kind = ErrorKind.UNKNOWN
    advice = {
        ErrorKind.CONTEXT_LIMIT: "服务端报告上下文超限",
        ErrorKind.CONFIGURATION: "请检查地址、认证、模型、额度和请求参数，不自动重试",
        ErrorKind.TRANSIENT: "服务暂时不可用或请求受限",
        ErrorKind.UNKNOWN: "错误原因不明确，停止自动重试",
    }[kind]
    known_code = code if code in _CONTEXT_CODES | _CONFIG_CODES | _TRANSIENT_CODES else None
    label = f"HTTP {status}" if status is not None else "API 错误"
    return ModelError(f"模型接口返回 {label}；{advice}", kind=kind, status=status,
                      provider_code=known_code, retry_after=retry_after)


@dataclass
class ModelConfig:
    endpoint: str
    model: str
    api_key: str = field(default="", repr=False)
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    timeout: float = 60.0

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("MODEL_API_URL 必须是无内嵌账号密码的完整 HTTP(S) 接口地址")
        # 兼容常见基础地址；其他自定义路径保持原样，不改动 .env。
        if url.path.rstrip("/") in {"", "/v1"}:
            self.endpoint = urlunsplit((url.scheme, url.netloc, "/v1/chat/completions", url.query, url.fragment))
        if not self.model.strip():
            raise ValueError("必须配置 MODEL_NAME")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MODEL_TIMEOUT 必须是有限正数")
        if not isinstance(self.extra_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.extra_headers.items()
        ):
            raise ValueError("MODEL_EXTRA_HEADERS 必须是字符串键值组成的 JSON 对象")

    @classmethod
    def from_env(cls) -> "ModelConfig":
        # 固定读取 backend/.env，不依赖 IDE 的工作目录；已有环境变量优先。
        load_dotenv(ENV_FILE, override=False)
        try:
            headers = json.loads(os.environ.get("MODEL_EXTRA_HEADERS", "{}"))
            timeout = float(os.environ.get("MODEL_TIMEOUT", "60"))
        except (ValueError, TypeError):
            raise ValueError("MODEL_EXTRA_HEADERS 或 MODEL_TIMEOUT 格式错误") from None
        return cls(
            endpoint=os.environ.get("MODEL_API_URL", ""),
            model=os.environ.get("MODEL_NAME", ""),
            api_key=os.environ.get("MODEL_API_KEY", ""),
            auth_header=os.environ.get("MODEL_AUTH_HEADER", "Authorization"),
            auth_prefix=os.environ.get("MODEL_AUTH_PREFIX", "Bearer "),
            extra_headers=headers,
            timeout=timeout,
        )


def parse_response(payload: dict) -> Message:
    try:
        choice = payload["choices"][0]
        reason = choice["finish_reason"]
        if reason == "length":
            raise ModelError("模型输出被截断，请检查输出上限和窗口配置；不执行不完整代码", kind=ErrorKind.OUTPUT_LIMIT)
        if reason == "content_filter":
            raise ModelError("模型响应被过滤；请调整任务内容", kind=ErrorKind.CONTENT_FILTER)
        if reason not in {"stop", "tool_calls"}:
            raise ValueError("unexpected finish reason")
        raw = choice["message"]
        if raw.get("refusal"):
            raise ModelError("模型拒绝此请求", kind=ErrorKind.CONTENT_FILTER)
        if raw["role"] != "assistant":
            raise ValueError("unexpected role")
        content = raw.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError("unexpected content")
        calls = []
        ids = set()
        for item in raw.get("tool_calls") or []:
            function = item["function"]
            call_id, name = item["id"], function["name"]
            arguments = json.loads(function["arguments"])
            json.dumps(arguments, allow_nan=False)  # Python 的解析器默认接受 NaN，不能将其写入历史。
            if (item["type"] != "function" or not isinstance(call_id, str)
                    or not call_id or call_id in ids or not isinstance(name, str)
                    or not name or not isinstance(arguments, dict)):
                raise ValueError("invalid tool call")
            ids.add(call_id)
            calls.append(ToolCall(name=name, arguments=arguments, id=call_id))
        if not calls and not content.strip():
            raise ValueError("empty response")
        if choice["finish_reason"] == "tool_calls" and not calls:
            raise ValueError("missing tool calls")
        return Message(Role.ASSISTANT, content=content, tool_calls=calls)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ModelError("模型响应为空或格式错误，需要 Chat Completions 兼容的消息与工具调用格式",
                         kind=ErrorKind.RESPONSE_INVALID) from None


class ChatClient:
    def __init__(self, config: ModelConfig, *, max_output_tokens: int | None = None,
                 retry_policy: RetryPolicy | None = None) -> None:
        self.config = config
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens <= 0):
            raise ValueError("模型输出上限必须为正整数")
        self.max_output_tokens = max_output_tokens
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()

    @property
    def tool_definitions(self) -> list[dict]:
        return deepcopy([RUN_CODE_TOOL])

    def complete(self, messages: list[Message]) -> Message:
        """保留原来的只返回 Message 的接口；主循环使用带 usage 的接口。"""
        return self.complete_with_usage(messages).message

    def complete_with_usage(self, messages: list[Message], *, max_output_tokens: int | None = None) -> ModelReply:
        payload = self._request(messages, with_tools=True,
                                max_output_tokens=self.max_output_tokens if max_output_tokens is None else max_output_tokens)
        reply = parse_response(payload)
        usage_error = None
        try:
            usage = parse_usage(payload)
        except (ValueError, TypeError):
            usage = None
            usage_error = "usage 格式无效，保留本次发送前估算，不为统计重新请求模型"
        response_id = payload.get("id")
        return ModelReply(reply, usage, usage_error, response_id if isinstance(response_id, str) else None)

    def summarize(self, messages: list[Message], *, max_output_tokens: int) -> str:
        """独立无工具请求，仅返回摘要文本，不触发任何工具执行。"""
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("摘要输出预算必须是正整数")
        if any(message.tool_calls or message.tool_call_id is not None for message in messages):
            raise ValueError("摘要请求只接受文本消息，原工具记录应作为待总结的数据")
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                payload = self._request(messages, with_tools=False, max_output_tokens=max_output_tokens)
                reply = parse_response(payload)
                if reply.tool_calls:
                    raise ModelError("摘要请求返回了工具调用，拒绝使用", kind=ErrorKind.RESPONSE_INVALID)
                return reply.content.strip()
            except ModelError as error:
                # 摘要独立限次重试；其自身超限时直接停止，不递归压缩摘要请求。
                delay = self.retry_policy.delay(attempt, error)
                if delay is None:
                    raise
                time.sleep(delay)

    def _request(
        self, messages: list[Message], *, with_tools: bool,
        max_output_tokens: int | None = None,
    ) -> dict:
        config = self.config
        headers = {"Content-Type": "application/json", **config.extra_headers}
        if config.api_key:
            headers[config.auth_header] = config.auth_prefix + config.api_key
        body = {
            "model": config.model,
            "messages": [message_to_dict(message) for message in messages],
            "stream": False,
        }
        if with_tools:
            body.update(tools=self.tool_definitions, tool_choice="auto")
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        try:
            request = Request(
                config.endpoint,
                data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
        except (ValueError, UnicodeError, TypeError):
            raise ModelError("请求配置或消息编码无效，请检查地址、请求头与参数", kind=ErrorKind.CONFIGURATION) from None
        try:
            with urlopen(request, timeout=config.timeout) as response:
                # 防止错误网关返回无限正文；不在错误信息中打印正文。
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ModelError("模型响应超过 2 MB 上限")
        except HTTPError as error:
            try:
                status = error.code
                wait = _retry_after(error.headers.get("Retry-After") if error.headers else None)
                try:
                    payload = json.loads(error.read(65_536))
                except (ValueError, OSError, HTTPException):
                    payload = {}
            finally:
                error.close()
            raise _api_error(payload, status, wait) from None
        except (ssl.SSLError, ssl.CertificateError):
            raise ModelError("TLS 配置或证书验证失败，请检查服务配置", kind=ErrorKind.CONFIGURATION) from None
        except URLError as error:
            kind = ErrorKind.CONFIGURATION if isinstance(error.reason, ssl.SSLError) else ErrorKind.TRANSIENT
            raise ModelError("模型接口连接失败，请检查网络和 TLS 配置", kind=kind) from None
        except InvalidURL:
            raise ModelError("HTTP 请求地址格式无效", kind=ErrorKind.CONFIGURATION) from None
        except (TimeoutError, OSError, HTTPException):
            raise ModelError("模型接口连接中断或超时", kind=ErrorKind.TRANSIENT) from None
        except (ValueError, UnicodeError):
            raise ModelError("HTTP 请求配置无效，请检查地址和请求头", kind=ErrorKind.CONFIGURATION) from None

        if not data.strip():
            raise ModelError("模型接口返回空正文", kind=ErrorKind.RESPONSE_INVALID)
        prefix = data.lstrip()[:40].lower()
        if prefix.startswith((b"<!doctype html", b"<html")):
            raise ModelError("模型接口返回 HTML 网页；请配置实际的 Chat Completions 地址", kind=ErrorKind.CONFIGURATION)
        if prefix.startswith((b"data:", b"event:")):
            raise ModelError("模型接口返回 SSE 流，但请求为 stream=False；请检查服务协议", kind=ErrorKind.CONFIGURATION)
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise ModelError(
                f"模型响应不是有效 JSON（第 {error.lineno} 行，第 {error.colno} 列）；"
                "响应正文未输出", kind=ErrorKind.RESPONSE_INVALID,
            ) from None
        except UnicodeError:
            raise ModelError("模型响应编码无效，无法按 JSON 读取", kind=ErrorKind.RESPONSE_INVALID) from None
        if not isinstance(payload, dict):
            raise ModelError("模型响应必须是 JSON 对象", kind=ErrorKind.RESPONSE_INVALID)
        if "error" in payload:
            raise _api_error(payload, None)
        return payload
