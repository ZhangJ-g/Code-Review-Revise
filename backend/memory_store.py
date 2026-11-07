"""一段对话一个 JSON 文件，保存元数据与全部历史分支。"""

import json
import fcntl
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .history import History, excerpt, require_timestamp
from .models import Compaction, Conversation, LongTermMemory, Message, Role, Session, TaskRun, ToolCall
from .paths import MEMORY_DIR


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
    tasks = []
    for task in history.tasks.values():
        data = asdict(task)
        for key in ("created_at", "updated_at", "finished_at"):
            data[key] = data[key].isoformat() if data[key] is not None else None
        tasks.append(data)
    compactions = []
    for record in history.compactions.values():
        data = asdict(record)
        data["created_at"] = record.created_at.isoformat()
        compactions.append(data)
    return {"schema_version": 1, "conversation": metadata, "nodes": nodes,
            "tasks": tasks, "compactions": compactions}


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
        raw_tasks = data.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raise ValueError("tasks 必须是列表")
        tasks = []
        for raw_task in raw_tasks:
            task = dict(raw_task)
            for key in ("id", "user_node_id", "checkpoint_node_id", "status", "phase", "active_tool_call_id",
                        "rounds", "error", "created_at", "updated_at", "finished_at"):
                if key not in task:
                    raise ValueError("任务检查点缺少必要字段")
            for key in ("created_at", "updated_at"):
                task[key] = _time_from_json(task[key])
            if task["finished_at"] is not None:
                task["finished_at"] = _time_from_json(task["finished_at"])
            tasks.append(TaskRun(**task))
        raw_compactions = data.get("compactions", [])
        if not isinstance(raw_compactions, list):
            raise ValueError("compactions 必须是列表")
        compactions = []
        for raw_record in raw_compactions:
            record = dict(raw_record)
            for key in ("id", "through_node_id", "summary", "created_at"):
                if key not in record:
                    raise ValueError("压缩记录缺少必要字段")
            record["created_at"] = _time_from_json(record["created_at"])
            compactions.append(Compaction(**record))
        history = History(conversation, nodes, tasks, compactions)
        # 没有用户消息的空对话保留空标题，首次提交时由 History.append 自动命名。
        if not history.conversation.title.strip():
            users = [node for node in nodes if node.message.role == Role.USER]
            first_user = min(users, key=lambda node: (node.created_at, node.id)) if users else None
            history.conversation.title = excerpt(first_user.message.content) if first_user else ""
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
            MEMORY_DIR if directory is None else Path(directory).resolve()
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


class MemoryConflict(ValueError):
    """记忆已被其他请求修改，需要重新加载，避免覆盖别人的修改。"""


def validate_memory(memory: LongTermMemory) -> None:
    """检查一条记忆的 ID、名称、正文长度和时间是否有效。"""
    if not isinstance(memory.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", memory.id):
        raise ValueError("记忆 ID 无效")
    if not isinstance(memory.name, str) or not memory.name.strip() or len(memory.name) > 120:
        raise ValueError("记忆名称必须是 1～120 个字符")
    if not isinstance(memory.content, str) or len(memory.content) > 32000:
        raise ValueError("记忆内容必须是最多 32000 个字符的 Markdown 文本")
    require_timestamp(memory.created_at)
    require_timestamp(memory.updated_at)
    if memory.updated_at < memory.created_at:
        raise ValueError("记忆更新时间不能早于创建时间")


def memory_catalog_to_dict(revision: int, active_id: str | None, memories) -> dict:
    """检查整个记忆目录，并转成 JSON 能保存的字典；时间转成字符串。"""
    if type(revision) is not int or revision < 0:
        raise ValueError("记忆修订号无效")
    rows, ids = [], set()
    for memory in memories:
        validate_memory(memory)
        if memory.id in ids:
            raise ValueError("记忆 ID 重复")
        ids.add(memory.id)
        row = asdict(memory)
        for key in ("created_at", "updated_at"):
            row[key] = row[key].isoformat()
        rows.append(row)
    if len(rows) > 100:
        raise ValueError("最多保存 100 条记忆")
    if active_id is not None and (not isinstance(active_id, str) or active_id not in ids):
        raise ValueError("当前应用的记忆不存在")
    return {"version": 1, "revision": revision, "active_memory_id": active_id, "memories": rows}


class PersistentMemoryStore:
    """负责读写长期记忆 JSON 文件，不负责选择当前应用的记忆。

    管理器启动时调用 load()，用户修改时调用 save()；模型请求使用内存数据。
    """

    def __init__(self, directory: str | Path = MEMORY_DIR) -> None:
        self.path = Path(directory) / "long_term" / "memories.json"
        self._signature = None

    def _current_signature(self):
        """文件标记：文件编号、最后修改时间和大小，用来发现外部修改。"""
        try:
            stat = self.path.stat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def load(self) -> tuple[int, str | None, tuple[LongTermMemory, ...]]:
        """读取并检查 JSON，返回目录版本、当前记忆 ID 和全部条目。"""
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                stat = os.fstat(handle.fileno())
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                if stat.st_size > 2_000_000:
                    raise ValueError("记忆文件超过 2 MB 上限")
                text = handle.read(2_000_001)
        except FileNotFoundError:
            self._signature = None
            return 0, None, ()
        try:
            if len(text) > 2_000_000:
                raise ValueError("记忆文件超过 2 MB 上限")
            data = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                              parse_constant=_reject_nonfinite_number)
            if (not isinstance(data, dict) or type(data["version"]) is not int
                    or data["version"] != 1 or not isinstance(data["memories"], list)):
                raise ValueError("记忆文件格式或版本无效")
            records = []
            for raw in data["memories"]:
                if not isinstance(raw, dict) or set(raw) != {"id", "name", "content", "created_at", "updated_at"}:
                    raise ValueError("记忆字段缺失或包含不支持的字段")
                row = dict(raw)
                for key in ("created_at", "updated_at"):
                    row[key] = _time_from_json(row[key])
                records.append(LongTermMemory(**row))
            revision = data.get("revision", 0)
            memory_catalog_to_dict(revision, data["active_memory_id"], records)
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            raise ValueError("长期记忆 JSON 缺少字段或格式无效，未覆盖文件") from None
        if self._current_signature() != signature:
            raise MemoryConflict("记忆文件在加载期间发生变化，请重新启动服务")
        self._signature = signature
        return revision, data["active_memory_id"], tuple(records)

    def save(self, revision: int, active_id: str | None, memories) -> None:
        """先写好临时文件，再整体替换旧文件，避免只保存半份 JSON。"""
        data = memory_catalog_to_dict(revision, active_id, memories)
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if len(text.encode("utf-8")) > 2_000_000:
            raise ValueError("记忆文件超过 2 MB 上限")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 文件锁：同一时间只让一个进程保存；文件标记用于发现其他程序的修改。
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._current_signature() != self._signature:
                raise MemoryConflict("记忆文件已被其他实例或外部程序修改，请重启后重新加载")
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".memories-", suffix=".tmp", delete=False) as handle:
                    temp_path = Path(handle.name)
                    handle.write(text)
                    handle.flush()
                    # fsync：要求系统把临时文件的内容写入磁盘，再替换旧文件。
                    os.fsync(handle.fileno())
                    stat = os.fstat(handle.fileno())
                    signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                os.replace(temp_path, self.path)
                # 替换成功就表示文件已保存；直接记下标记，不再额外查询文件。
                self._signature = signature
            finally:
                if temp_path is not None and temp_path.exists():
                    temp_path.unlink()
