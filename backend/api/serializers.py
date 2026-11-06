"""将现有历史转换为页面快照；不保存历史、不发模型请求、不执行工具。"""

import hashlib
import json
from dataclasses import asdict, replace

from ..compaction_manager import CompactionError, CompactionManager
from ..agent import Agent
from ..context_manager import ContextConfig, ContextManager
from ..history import History
from ..llm import RUN_CODE_TOOL
from ..message_utils import message_to_dict
from ..models import Role
from ..memory_store import history_to_dict
from ..token_stats import estimate_request_tokens


class _ReadOnlyCapabilities:
    """查询仅可使用 select/build_context，意外调用生成或保存时立即拒绝。"""

    def save(self, history: History) -> None:
        raise PermissionError("历史浏览接口不允许保存")

    def summarize(self, messages, *, max_output_tokens: int) -> str:
        raise PermissionError("历史浏览接口不允许生成摘要")


def conversation_dict(conversation) -> dict:
    data = asdict(conversation)
    for key in ("created_at", "updated_at"):
        data[key] = data[key].isoformat()
    return data


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def history_revision(history: History) -> str:
    """写入版本只取完整持久化快照，不包含浏览分支、usage 或进程状态。"""
    return fingerprint(history_to_dict(history))


def _metrics(history: History, task, config: ContextConfig | None, config_error: str | None) -> dict:
    tools = [RUN_CODE_TOOL]
    ratio = config.tokens_per_char if config else 2.0
    raw = estimate_request_tokens([message_to_dict(entry.message) for entry in history.path()],
                                  tools, tokens_per_char=ratio).estimated_input_tokens
    estimated, compaction_id, source = raw, None, "raw_history"
    warning = config_error
    if config is not None and task is not None:
        readonly = _ReadOnlyCapabilities()
        compactor = CompactionManager(
            readonly, readonly, max_input_tokens=config.summary_input_budget,
            max_summary_tokens=config.summary_output_limit, tokens_per_char=ratio,
        )
        try:
            # inspect 只重建视图；不能用会触发摘要请求的 prepare。
            view = ContextManager(config, compactor, tools).inspect(history, task.id)
            estimated = view.estimate.estimated_input_tokens
            compaction_id, source = view.compaction_id, "context_preview"
        except CompactionError:
            warning = "无法确定安全摘要范围，显示原始分支估算。"
    budget = config.input_budget if config else None
    return {
        "estimated_input_tokens": estimated, "raw_input_tokens": raw,
        "input_budget": budget, "context_window": config.context_window if config else None,
        "max_output_tokens": config.max_output_tokens if config else None,
        "tokens_per_char": ratio, "compaction_id": compaction_id, "source": source,
        "over_budget": estimated > budget if budget is not None else None,
        "usage": None, "warning": warning,
        "usage_note": "历史文件未保存 API usage，无法恢复实测用量。",
    }


def conversation_snapshot(
    history: History, *, leaf_id: str | None = None,
    config: ContextConfig | None = None, config_error: str | None = None,
) -> dict:
    """可浏览其他叶节点，但不移动原 History 指针，不写回默认分支。"""
    selected = leaf_id if leaf_id is not None else history.conversation.current_leaf_id
    if selected not in history.nodes or history.nodes[selected].children_ids:
        raise ValueError("只能浏览现有分支的叶节点")
    entries = history.path(selected)
    positions = {entry.id: index for index, entry in enumerate(entries)}
    results = {}
    for entry in entries:
        message = entry.message
        if message.role != Role.TOOL:
            continue
        try:
            result = json.loads(message.content)
        except (TypeError, ValueError):
            result = None
        results[message.tool_call_id] = result if isinstance(result, dict) else {"status": "recorded"}

    messages = []
    for entry in entries:
        message = entry.message
        try:
            Agent.validate_resume(history, parent_id=entry.id)
            can_fork, fork_reason = True, None
        except ValueError as error:
            can_fork, fork_reason = False, str(error)
        messages.append({
            "id": entry.id, "parent_id": entry.parent_id, "created_at": entry.created_at.isoformat(),
            "role": message.role.value, "content": message.content, "tool_call_id": message.tool_call_id,
            "can_fork": can_fork, "fork_reason": fork_reason,
            "tool_result": results.get(message.tool_call_id) if message.role == Role.TOOL else None,
            "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments,
                            "result": results.get(call.id)} for call in message.tool_calls],
        })
    tasks = []
    for task in history.tasks.values():
        if task.checkpoint_node_id not in positions or task.user_node_id not in positions:
            continue
        data = asdict(task)
        for key in ("created_at", "updated_at", "finished_at"):
            data[key] = data[key].isoformat() if data[key] is not None else None
        data["description"] = history.nodes[task.user_node_id].message.content
        data["can_recover"] = False
        if task.status not in {"completed", "cancelled"}:
            try:
                Agent.validate_recovery(history, task.id)
                data["can_recover"] = True
            except ValueError:
                pass
        tasks.append(data)
    tasks.sort(key=lambda item: positions[item["checkpoint_node_id"]])
    current_task = next((task for task in history.tasks.values() if task.checkpoint_node_id == selected), None)

    # 只给读取器一份独立 Conversation；原始指针和所有历史节点均不修改。
    view_history = History(replace(history.conversation, current_leaf_id=selected),
                           list(history.nodes.values()), list(history.tasks.values()),
                           list(history.compactions.values()))
    metrics = _metrics(view_history, current_task, config, config_error)
    compactions = [{"id": record.id, "through_node_id": record.through_node_id,
                    "summary": record.summary, "created_at": record.created_at.isoformat(),
                    "base_compaction_id": record.base_compaction_id,
                    "selected_for_preview": record.id == metrics["compaction_id"]}
                   for record in history.compactions.values() if record.through_node_id in positions]
    compactions.sort(key=lambda item: positions[item["through_node_id"]], reverse=True)
    branches = []
    for branch in history.branches():
        data = asdict(branch)
        data["last_message_at"] = branch.last_message_at.isoformat()
        data["is_selected"] = branch.leaf_id == selected
        branches.append(data)
    data = {
        "conversation": conversation_dict(history.conversation), "selected_leaf_id": selected,
        "messages": messages, "branches": branches, "tasks": tasks, "metrics": metrics,
        "compactions": compactions,
        "runtime": {"execution_connected": False, "worker_alive": None, "source": "saved_checkpoint"},
        "history_revision": history_revision(history),
    }
    try:
        Agent.validate_resume(history, parent_id=selected)
        data["can_submit"], data["submit_reason"] = True, None
    except ValueError as error:
        data["can_submit"], data["submit_reason"] = False, str(error)
    data["revision"] = fingerprint(data)
    return data
