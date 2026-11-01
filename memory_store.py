"""一段对话一个 JSON 文件，保存元数据与全部历史分支。"""

import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from history import History, excerpt, require_timestamp
from models import Conversation, Message, Role, Session, ToolCall


def _time_from_json(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间字段必须是 ISO 8601 字符串")
    result = datetime.fromisoformat(value)
    require_timestamp(result)
    return result


def _message_from_json(data: dict) -> Message:
    if not isinstance(data, dict) or not isinstance(data["content"], str):
        raise ValueError("消息必须包含字符串 content")
    if not isinstance(data["tool_calls"], list):
        raise ValueError("tool_calls 必须是列表")
    calls = []
    ids = set()
    for call in data["tool_calls"]:
        if (not isinstance(call, dict) or not isinstance(call["id"], str)
                or not call["id"].strip() or call["id"] in ids
                or not isinstance(call["name"], str) or not call["name"].strip()
                or not isinstance(call["arguments"], dict)):
            raise ValueError("工具调用字段无效或 ID 重复")
        ids.add(call["id"])
        calls.append(ToolCall(**call))
    if data["tool_call_id"] is not None and (
        not isinstance(data["tool_call_id"], str) or not data["tool_call_id"].strip()
    ):
        raise ValueError("tool_call_id 必须是非空字符串或 null")
    return Message(Role(data["role"]), data["content"], calls, data["tool_call_id"])


def history_to_dict(history: History) -> dict:
    history.validate()
    metadata = asdict(history.conversation)
    for key in ("created_at", "updated_at"):
        metadata[key] = metadata[key].isoformat()
    nodes = []
    for entry in history.nodes.values():
        data = asdict(entry)
        data["created_at"] = entry.created_at.isoformat()
        data["message"]["role"] = entry.message.role.value
        # 保存前同样检查消息字段，避免写入无法重新加载的文件。
        _message_from_json(data["message"])
        nodes.append(data)
    return {"schema_version": 1, "conversation": metadata, "nodes": nodes}


def history_from_dict(data: dict) -> History:
    try:
        if not isinstance(data, dict) or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("不支持的历史文件版本")
        metadata = dict(data["conversation"])
        for key in ("id", "root_id", "current_leaf_id", "created_at", "updated_at"):
            if key not in metadata:
                raise ValueError("对话元数据缺少必要字段")
        for key in ("created_at", "updated_at"):
            metadata[key] = _time_from_json(metadata[key])
        conversation = Conversation(**metadata)
        if not isinstance(data["nodes"], list):
            raise ValueError("nodes 必须是列表")
        nodes = []
        for raw in data["nodes"]:
            node = dict(raw)
            for key in ("id", "parent_id", "children_ids", "created_at", "message"):
                if key not in node:
                    raise ValueError("历史节点缺少必要字段")
            node["created_at"] = _time_from_json(node["created_at"])
            node["message"] = _message_from_json(node["message"])
            nodes.append(Session(**node))
        history = History(conversation, nodes)
        # 旧文件没有 title 时从首条用户消息恢复展示标题，不在读取时改写文件。
        if not history.conversation.title.strip():
            users = [node for node in nodes if node.message.role == Role.USER]
            first_user = min(users, key=lambda node: (node.created_at, node.id)) if users else None
            history.conversation.title = excerpt(first_user.message.content) if first_user else "未命名对话"
        return history
    except (KeyError, TypeError, AttributeError):
        raise ValueError("历史 JSON 缺少必要字段或字段类型错误") from None


def _reject_duplicate_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("历史 JSON 对象存在重复字段")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> None:
    raise ValueError("历史 JSON 不允许 NaN 或 Infinity")


class JsonMemoryStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (
            Path(__file__).resolve().parent / "memory" if directory is None else Path(directory).resolve()
        )

    def path_for(self, conversation_id: str) -> Path:
        # 对话 ID 直接参与文件名，拒绝路径穿越。
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
            raise ValueError("对话文件 ID 只能包含字母、数字、下划线或连字符，最长 128 字符")
        return self.directory / f"{conversation_id}.json"

    def list_conversations(self) -> tuple[list[Conversation], list[str]]:
        """小规模本地存储直接加载校验，损坏文件单独提示，不影响其他对话。"""
        conversations = []
        notices = []
        for path in sorted(self.directory.glob("*.json")):
            if not path.is_file():
                continue
            try:
                history = self.load(path.stem)
            except (ValueError, OSError):
                notices.append(f"已跳过无法读取或校验失败的文件：{path.name}")
                continue
            conversations.append(history.conversation)
        conversations.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return conversations, notices

    def save(self, history: History) -> Path:
        target = self.path_for(history.conversation.id)
        data = history_to_dict(history)
        # 在替换前完成编码，避免编码失败影响已有文件。
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        self.directory.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=".history-", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        return target

    def load(self, conversation_id: str) -> History:
        target = self.path_for(conversation_id)
        try:
            data = json.loads(
                target.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_number,
            )
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("历史文件不是有效的 UTF-8 JSON，未加载或覆盖文件") from None
        history = history_from_dict(data)
        if history.conversation.id != conversation_id:
            raise ValueError("文件名与对话 ID 不一致")
        return history
