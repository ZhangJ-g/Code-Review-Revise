"""代码检查修复助手的消息与历史节点。
数据结构
Session 在这里表示一条历史条目，而不是整段会话。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """助手提出的工具调用；id 用于关联后续工具结果。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)


@dataclass
class Message:
    """系统提示词、用户输入、助手回复或工具返回结果。"""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        # 允许从持久化数据中的字符串恢复角色，同时拒绝未知角色。
        self.role = Role(self.role)
        if self.tool_calls and self.role != Role.ASSISTANT:
            raise ValueError("只有助手消息可以发起工具调用")
        if self.role == Role.TOOL:
            if not self.tool_call_id:
                raise ValueError("工具结果必须通过 tool_call_id 关联工具调用")
        elif self.tool_call_id is not None:
            raise ValueError("只有工具结果可以设置 tool_call_id")


@dataclass
class Session:
    """包含一条消息的历史节点；根节点的 parent_id 为 None。

    children_ids 支持从同一历史位置产生不同分支。父子关系的一致性
    留给后续历史管理模块维护，数据结构本身不负责查找或连接节点。
    """

    message: Message
    id: str = field(default_factory=new_id)
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class Conversation:#记录整棵对话树的元数据
    """整段对话的元数据；具体消息仍由 Session 节点保存。"""

    root_id: str
    current_leaf_id: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    title: str = ""


@dataclass
class TaskRun:
    """一次用户任务的执行检查点；恢复时复用 id 和原用户消息。"""

    user_node_id: str
    checkpoint_node_id: str
    id: str = field(default_factory=new_id)
    status: str = "running"
    phase: str = "waiting_model"
    active_tool_call_id: str | None = None
    rounds: int = 0
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
