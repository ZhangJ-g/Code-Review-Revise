"""注入实际搜索服务；未配置时不向模型暴露搜索工具。"""

from typing import Callable

from .base import Tool, ToolRegistry


def register_search_tool(
    registry: ToolRegistry, search: Callable[[str], str]
) -> None:
    """search 应为同步函数，由调用方负责服务认证和请求超时。"""
    def web_search(query: str) -> str:
        return search(query)

    registry.register(Tool("web_search", "搜索互联网，返回搜索服务提供的文本结果", web_search))
