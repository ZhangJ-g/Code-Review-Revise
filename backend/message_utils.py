"""纯消息工具：API 编码及工具调用配对；不依赖模型客户端或 Agent。"""

import json
from collections.abc import Sequence

from .models import Message, Role, ToolCall


def message_to_dict(message: Message) -> dict:
    """
     封装数据给大模型使用
    """
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


def _scan(messages: Sequence[Message]) -> tuple[list[ToolCall], set[int]]:
    """扫描历史消息，找出工具调用与工具执行结果都完整的切分位置。
    list[ToolCall]	尚未匹配到执行结果的工具调用
    set[int]	可以安全切分的历史位置"""
    if not messages or messages[0].role != Role.SYSTEM:
        raise ValueError("消息路径必须以系统消息为根")
    pending: dict[str, ToolCall] = {}
    # pending = {Assistant:
    #     tool_calls:
    #         - id: call_A
    #         - id: call_B
    #     这时候两个工具都还没有返回结果。
    #     "call_A": ToolCall(...),
    #     "call_B": ToolCall(...)
    # }
    seen = set()#记录历史上出现过的工具调用 ID
    boundaries = {0}#处理完第几个位置的消息之后，可以安全切分。
    for index, message in enumerate(messages[1:], 1):
        if message.role == Role.SYSTEM:
            raise ValueError("系统消息只能位于历史根节点")
        if pending:#如果有没处理完的任务
            if message.role != Role.TOOL or message.tool_call_id not in pending:
                raise ValueError("历史工具调用与结果不匹配")
            del pending[message.tool_call_id]
        elif message.role == Role.TOOL:
            raise ValueError("工具结果没有匹配的调用")
        elif message.tool_calls:
            ids = [call.id for call in message.tool_calls]
            if any(not isinstance(call_id, str) or not call_id.strip() for call_id in ids):
                raise ValueError("工具调用 ID 必须是非空字符串")
            if len(ids) != len(set(ids)) or seen.intersection(ids):
                raise ValueError("当前分支存在重复工具调用 ID")
            pending = {call.id: call for call in message.tool_calls}
            seen.update(ids)
        if not pending:
            """意思是：处理完当前消息后，如果没有等待结果的工具调用，就把当前位置记为候选切分点。主要看处理之后pending的状态如何 是空的就可以记录为可处理边界"""
            boundaries.add(index)
    return list(pending.values()), boundaries


def pending_tool_calls(messages: Sequence[Message]) -> list[ToolCall]:
    """允许末尾仍有待执行调用，返回尚未匹配结果的调用列表。获取历史记录中尚未得到执行结果的工具调用。"""
    return _scan(messages)[0]


def tool_boundary_indices(messages: Sequence[Message]) -> set[int]:
    """可在其后切分的索引：此前没有尚待匹配结果的工具调用。获取历史记录中可以安全切分的位置，避免把工具调用和执行结果拆散。"""
    return _scan(messages)[1]
