"""注册已经连接的 MCP 工具的同步调用适配器，不负责连接 MCP 服务。"""

from typing import Callable

from .base import Tool, ToolRegistry


def register_mcp_tool(
    registry: ToolRegistry,
    name: str,
    description: str,
    invoke: Callable,
) -> None:
    """invoke 的参数签名应体现该 MCP 工具的真实参数。"""
    registry.register(Tool(name, description, invoke))
