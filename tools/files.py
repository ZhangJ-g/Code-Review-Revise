"""项目目录内的基本文件操作；这里的路径检查不是 Python 沙箱。"""

from pathlib import Path

from .base import Tool, ToolRegistry


def register_file_tools(registry: ToolRegistry, root: str | Path) -> None:
    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir():
        raise ValueError("工具根路径必须是目录")

    def resolve_path(path: str) -> Path:
        target = (project_root / path).resolve()
        if not target.is_relative_to(project_root):
            raise ValueError("不允许访问项目目录外的路径")
        return target

    def read_file(path: str) -> str:
        return resolve_path(path).read_text(encoding="utf-8")

    def list_directory(path: str = ".") -> list[str]:
        target = resolve_path(path)
        return sorted(
            str(item.relative_to(project_root)) + ("/" if item.is_dir() else "")
            for item in target.iterdir()
        )

    def write_file(path: str, content: str, overwrite: bool = False) -> str:
        target = resolve_path(path)
        # 默认独占创建，避免无意覆盖；父目录需已存在。
        with target.open("w" if overwrite else "x", encoding="utf-8") as handle:
            handle.write(content)
        return str(target.relative_to(project_root))

    registry.register(Tool("read_file", "读取项目内的 UTF-8 文件", read_file))
    registry.register(Tool("list_directory", "列出项目内目录的直接子项", list_directory))
    registry.register(Tool("write_file", "写入 UTF-8 文件，覆盖已有文件需 overwrite=True", write_file))
