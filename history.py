"""历史树的结构校验、分支操作及当前上下文路径。"""

from datetime import datetime, timezone
from dataclasses import dataclass

from models import Conversation, Message, Role, Session, TaskRun


def excerpt(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass(frozen=True)
class BranchInfo:
    leaf_id: str
    last_user_message: str
    last_assistant_message: str
    last_message_at: datetime
    is_current: bool


def require_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("节点和对话 ID 必须是非空字符串")


def require_timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("时间必须是带时区的 datetime")


class History:
    def __init__(self, conversation: Conversation, nodes: list[Session], tasks: list[TaskRun] | None = None) -> None:
        self.conversation = conversation
        self.nodes: dict[str, Session] = {}
        for node in nodes:
            require_id(node.id)
            if node.id in self.nodes:
                raise ValueError("历史中存在重复节点 ID")
            self.nodes[node.id] = node
        self.tasks: dict[str, TaskRun] = {}
        for task in tasks or []:
            require_id(task.id)
            if task.id in self.tasks:
                raise ValueError("历史中存在重复任务 ID")
            self.tasks[task.id] = task
        self.validate()

    @classmethod
    def create(cls, system_message: Message) -> "History":
        root = Session(message=system_message)
        conversation = Conversation(root_id=root.id, current_leaf_id=root.id)
        return cls(conversation, [root])

    def validate(self) -> None:
        metadata = self.conversation
        if not isinstance(metadata.title, str):
            raise ValueError("对话标题必须是字符串")
        for value in (metadata.id, metadata.root_id, metadata.current_leaf_id):
            require_id(value)
        require_timestamp(metadata.created_at)
        require_timestamp(metadata.updated_at)
        if metadata.updated_at < metadata.created_at:
            raise ValueError("对话更新时间不能早于创建时间")
        #检查 metadata.root_id 这个值，是否不存在于 self.nodes 这个字典的键中。
        #前面对话，后面是树
        if metadata.root_id not in self.nodes:
            raise ValueError("root_id 指向不存在的节点")
        if metadata.current_leaf_id not in self.nodes:
            raise ValueError("current_leaf_id 指向不存在的节点")

        roots = []
        for node_id, node in self.nodes.items():
            require_id(node.id)
            require_timestamp(node.created_at)
            if node_id != node.id:
                raise ValueError("节点索引与节点 ID 不一致")
            if not isinstance(node.children_ids, list):
                raise ValueError("children_ids 必须是列表")
            for child_id in node.children_ids:
                require_id(child_id)
            if len(set(node.children_ids)) != len(node.children_ids):
                raise ValueError("子节点列表存在重复 ID")
            if node.parent_id is None:
                roots.append(node.id)
            else:
                require_id(node.parent_id)
                parent = self.nodes.get(node.parent_id)
                if parent is None or node.id not in parent.children_ids:
                    raise ValueError("父节点不存在，或父子引用不一致")
            for child_id in node.children_ids:
                child = self.nodes.get(child_id)
                if child is None or child.parent_id != node.id:
                    raise ValueError("子节点不存在，或父子引用不一致")
        if roots != [metadata.root_id]:
            raise ValueError("历史必须有且只有 root_id 指向的根节点")

        visited = set()
        pending = [metadata.root_id]
        while pending:
            node_id = pending.pop()
            if node_id in visited:
                raise ValueError("历史树存在环或重复路径")
            visited.add(node_id)
            pending.extend(self.nodes[node_id].children_ids)
        if len(visited) != len(self.nodes):
            raise ValueError("历史包含无法从根到达的节点（可能存在孤立环）")
        if self.nodes[metadata.current_leaf_id].children_ids:
            raise ValueError("current_leaf_id 必须指向叶节点")
        self._validate_tasks()

    def _validate_tasks(self) -> None:
        open_checkpoints = set()
        for task_id, task in self.tasks.items():
            for value in (task.id, task.user_node_id, task.checkpoint_node_id):
                require_id(value)
            if task_id != task.id:
                raise ValueError("任务索引与任务 ID 不一致")
            if task.status not in {"running", "interrupted", "needs_review", "completed", "cancelled", "limit"}:
                raise ValueError("未知任务状态")
            if task.phase not in {"waiting_model", "waiting_approval", "executing_tool", "finished"}:
                raise ValueError("未知任务阶段")
            if type(task.rounds) is not int or task.rounds < 0:
                raise ValueError("任务模型轮数必须是非负整数")
            if task.error is not None and not isinstance(task.error, str):
                raise ValueError("任务错误必须是字符串或 null")
            require_timestamp(task.created_at)
            require_timestamp(task.updated_at)
            if task.updated_at < task.created_at:
                raise ValueError("任务更新时间早于创建时间")
            finished = task.status in {"completed", "cancelled"}
            if finished != (task.phase == "finished") or finished != (task.finished_at is not None):
                raise ValueError("任务结束状态、阶段与结束时间不一致")
            if task.finished_at is not None:
                require_timestamp(task.finished_at)
                if task.finished_at < task.created_at:
                    raise ValueError("任务结束时间早于创建时间")
            user = self.nodes.get(task.user_node_id)
            if user is None or user.message.role != Role.USER or task.checkpoint_node_id not in self.nodes:
                raise ValueError("任务的用户节点或检查点无效")
            final_message = self.nodes[task.checkpoint_node_id].message
            if task.status == "completed" and (
                final_message.role != Role.ASSISTANT or final_message.tool_calls or not final_message.content.strip()
            ):
                raise ValueError("完成任务必须有已保存的助手最终回答")
            ancestor_ids = set()
            node_id = task.checkpoint_node_id
            while node_id is not None:
                ancestor_ids.add(node_id)
                if node_id == task.user_node_id:
                    break
                node_id = self.nodes[node_id].parent_id
            if task.user_node_id not in ancestor_ids:
                raise ValueError("任务检查点不在原用户任务的后代路径上")
            if task.status == "needs_review" and task.phase != "executing_tool":
                raise ValueError("待核对任务必须停在工具执行阶段")
            if task.status == "limit" and task.phase != "waiting_model":
                raise ValueError("轮数上限检查点必须停在模型等待阶段")
            if task.phase in {"waiting_model", "finished"}:
                if task.active_tool_call_id is not None:
                    raise ValueError("此任务阶段不能包含活动工具调用")
            else:
                require_id(task.active_tool_call_id)
                if not any(
                    call.id == task.active_tool_call_id
                    for node_id in ancestor_ids for call in self.nodes[node_id].message.tool_calls
                ):
                    raise ValueError("任务活动工具调用不在检查点路径中")
            if not finished:
                if task.checkpoint_node_id in open_checkpoints:
                    raise ValueError("同一检查点存在多个未结束任务")
                open_checkpoints.add(task.checkpoint_node_id)

    def unfinished_task(self, node_id: str | None = None) -> TaskRun | None:
        node_id = self.conversation.current_leaf_id if node_id is None else node_id
        return next((task for task in self.tasks.values()
                     if task.checkpoint_node_id == node_id and task.status not in {"completed", "cancelled"}), None)

    def append(self, message: Message, *, parent_id: str | None = None) -> Session:
        """默认追加到当前叶节点；指定历史父节点即可创建新分支。"""
        self.validate()
        parent_id = self.conversation.current_leaf_id if parent_id is None else parent_id
        if parent_id not in self.nodes:
            raise ValueError("不能在不存在的节点后追加历史")
        entry = Session(message=message, parent_id=parent_id)
        if entry.id in self.nodes:
            raise ValueError("新节点 ID 与已有节点重复")
        self.nodes[parent_id].children_ids.append(entry.id)
        self.nodes[entry.id] = entry
        if message.role == Role.USER and not self.conversation.title.strip():
            self.conversation.title = excerpt(message.content) or "未命名对话"
        self.conversation.current_leaf_id = entry.id
        self.conversation.updated_at = datetime.now(timezone.utc)
        return entry

    def select_leaf(self, leaf_id: str) -> None:
        """切换到已有分支；不会删除其他分支。"""
        self.validate()
        if leaf_id not in self.nodes or self.nodes[leaf_id].children_ids:
            raise ValueError("只能切换到存在的叶节点")
        self.conversation.current_leaf_id = leaf_id
        self.conversation.updated_at = datetime.now(timezone.utc)

    def path(self, node_id: str | None = None) -> list[Session]:
        """返回根到指定节点的路径，默认返回当前分支。"""
        self.validate()
        node_id = self.conversation.current_leaf_id if node_id is None else node_id
        if node_id not in self.nodes:
            raise ValueError("要提取上下文的节点不存在")
        entries = []
        while node_id is not None:
            entry = self.nodes[node_id]
            entries.append(entry)
            node_id = entry.parent_id
        return list(reversed(entries))

    def branches(self) -> list[BranchInfo]:
        """每个叶节点对应一个候选分支，描述来自该叶节点自己的祖先链。"""
        self.validate()
        branches = []
        for node in self.nodes.values():
            if node.children_ids:
                continue
            messages = [entry.message for entry in self.path(node.id)]
            user = next((message for message in reversed(messages) if message.role == Role.USER), None)
            assistant = next((message for message in reversed(messages) if message.role == Role.ASSISTANT), None)
            assistant_text = ""
            if assistant is not None:
                assistant_text = assistant.content
                if not assistant_text.strip() and assistant.tool_calls:
                    assistant_text = "[工具调用：" + ", ".join(call.name for call in assistant.tool_calls) + "]"
            branches.append(BranchInfo(
                leaf_id=node.id,
                last_user_message=user.content if user is not None else "",
                last_assistant_message=assistant_text,
                last_message_at=node.created_at,
                is_current=node.id == self.conversation.current_leaf_id,
            ))
        return sorted(branches, key=lambda branch: (branch.last_message_at, branch.leaf_id), reverse=True)
