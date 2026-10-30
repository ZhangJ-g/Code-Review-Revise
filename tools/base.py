"""工具定义和注册；工具名称也是生成代码中的函数名。"""

import inspect
import keyword
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    function: Callable

    def context_line(self) -> str:
        return f"{self.name}{inspect.signature(self.function)}：{self.description}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}#列表

    def register(self, tool: Tool) -> None:
        if (not tool.name.isidentifier() or keyword.iskeyword(tool.name)
                or tool.name.startswith("_") or tool.name == "print"):
            raise ValueError(f"工具名称不能作为可调用名称使用: {tool.name}")
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复: {tool.name}")
        if not callable(tool.function):
            raise TypeError("工具执行函数必须可调用")
        if not tool.description.strip():
            raise ValueError("工具需要一行简短描述")
        inspect.signature(tool.function)
        self._tools[tool.name] = tool

    def describe(self) -> str:
        return "\n".join(tool.context_line() for tool in self._tools.values())

    def namespace(self) -> dict[str, Callable]:
        return {name: tool.function for name, tool in self._tools.items()}
