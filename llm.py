"""可配置的 Chat Completions 兼容接口，使用 python-dotenv 加载本地配置。"""

import math
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from models import Message, Role, ToolCall
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


class ModelError(Exception):
    """接口请求或响应协议错误；错误消息不包含认证信息及响应正文。"""


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
        # 固定为项目目录，不依赖 IDE 的工作目录；已有环境变量优先。
        load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
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


def message_to_dict(message: Message) -> dict:
    data = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def parse_response(payload: dict) -> Message:
    try:
        choice = payload["choices"][0]
        if choice["finish_reason"] not in {"stop", "tool_calls"}:
            raise ModelError("模型响应未正常结束（可能被截断或过滤），未执行代码")
        raw = choice["message"]
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
            if (item["type"] != "function" or not isinstance(call_id, str)
                    or not call_id or call_id in ids or not isinstance(name, str)
                    or not name or not isinstance(arguments, dict)):
                raise ValueError("invalid tool call")
            ids.add(call_id)
            calls.append(ToolCall(name=name, arguments=arguments, id=call_id))
        if not calls and not content.strip():
            raise ModelError("模型返回空回答，未将其视为任务完成")
        if choice["finish_reason"] == "tool_calls" and not calls:
            raise ValueError("missing tool calls")
        return Message(Role.ASSISTANT, content=content, tool_calls=calls)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ModelError("模型响应格式错误，需要 Chat Completions 兼容的消息与工具调用格式") from None


class ChatClient:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def complete(self, messages: list[Message]) -> Message:
        config = self.config
        headers = {"Content-Type": "application/json", **config.extra_headers}
        if config.api_key:
            headers[config.auth_header] = config.auth_prefix + config.api_key
        body = {
            "model": config.model,
            "messages": [message_to_dict(message) for message in messages],
            "tools": [RUN_CODE_TOOL],
            "tool_choice": "auto",
            "stream": False,
        }
        try:
            request = Request(
                config.endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urlopen(request, timeout=config.timeout) as response:
                # 防止错误网关返回无限正文；不在错误信息中打印正文。
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ModelError("模型响应超过 2 MB 上限")
        except HTTPError as error:
            status = error.code
            error.close()
            raise ModelError(f"模型接口返回 HTTP {status}；请检查服务配置和权限") from None
        except (URLError, TimeoutError, OSError):
            raise ModelError("模型接口连接失败或超时；本轮未自动重试") from None
        except UnicodeError:
            raise ModelError("请求地址或请求头编码无效，请检查非 ASCII 字符；尚未解析响应") from None
        except ValueError:
            raise ModelError("HTTP 请求配置无效，请检查接口地址和请求头格式（例如换行字符）") from None

        if not data.strip():
            raise ModelError("模型接口返回空正文，请检查接口路径和网关配置")
        prefix = data.lstrip()[:40].lower()
        if prefix.startswith((b"<!doctype html", b"<html")):
            raise ModelError("模型接口返回 HTML 网页，通常是地址指向首页或登录页；请配置实际的 Chat Completions 接口")
        if prefix.startswith((b"data:", b"event:")):
            raise ModelError("模型接口返回 SSE 流，但当前请求使用 stream=False；请检查服务协议")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise ModelError(
                f"模型响应不是有效 JSON（第 {error.lineno} 行，第 {error.colno} 列）；"
                "请检查接口是否为 Chat Completions 地址，响应正文未输出"
            ) from None
        except UnicodeError:
            raise ModelError("模型响应编码无效，无法按 JSON 读取") from None
        return parse_response(payload)
