"""CLI 和 Web 服务共用的项目写入锁，锁文件不删除，进程退出释放句柄。"""

import fcntl
import os
from pathlib import Path

from .paths import PROJECT_ROOT


class ProjectBusyError(RuntimeError):
    pass


class ProjectLock:
    def __init__(self, path: Path | None = None, *, owner: str = "Agent") -> None:
        self.path = path if path is not None else PROJECT_ROOT / ".runtime" / "agent.lock"
        self.owner = owner
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ProjectBusyError("项目正在被其他 CLI 或 Web 进程占用；请先停止原入口。") from None
        except BaseException:
            handle.close()
            raise
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{self.owner} pid={os.getpid()}\n")
            handle.flush()
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, *args):
        if self._handle is not None:
            # fork 的执行子进程可能继承句柄；只 close，最后一个持有者退出才释放锁。
            self._handle.close()
            self._handle = None
