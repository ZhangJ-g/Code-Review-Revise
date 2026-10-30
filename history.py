"""历史树的结构校验、分支操作及当前上下文路径。"""

from datetime import datetime, timezone

from models import Conversation, Message, Session


def require_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("节点和对话 ID 必须是非空字符串")


def require_timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("时间必须是带时区的 datetime")


class History:
    def __init__(self, conversation: Conversation, nodes: list[Session]) -> None:
        self.conversation = conversation
        self.nodes: dict[str, Session] = {}
        for node in nodes:
            require_id(node.id)
            if node.id in self.nodes:
                raise ValueError("历史中存在重复节点 ID")
            self.nodes[node.id] = node
        self.validate()

    @classmethod
    def create(cls, system_message: Message) -> "History":
        root = Session(message=system_message)
        conversation = Conversation(root_id=root.id, current_leaf_id=root.id)
        return cls(conversation, [root])

    def validate(self) -> None:
        metadata = self.conversation
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
