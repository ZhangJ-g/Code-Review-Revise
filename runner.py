"""可信本地实验的代码执行器。独立进程和超时不提供权限隔离。

当前使用 Linux fork 继承同步工具函数，仅适用于单线程本地原型。
工具产生的文件或外部副作用不会随异常、超时自动回滚。
"""

import contextlib
import io
import multiprocessing
import traceback
from dataclasses import dataclass
from multiprocessing.connection import Connection

from tools import ToolRegistry


@dataclass
class RunResult:
    status: str
    output: str = ""
    error: str | None = None


class _LimitedOutput(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def write(self, text: str) -> int:
        remaining = self.limit - self.size
        if len(text) > remaining:
            self.truncated = True
        piece = text[:remaining]
        if piece:
            self.parts.append(piece)
            self.size += len(piece)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self.parts) + ("\n[输出已截断]" if self.truncated else "")


def _execute(code: str, namespace: dict, connection: Connection, limit: int) -> None:
    output = _LimitedOutput(limit)
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile(code, "<agent_code>", "exec"), namespace)
        result = RunResult("success", output.getvalue())
    except BaseException:
        result = RunResult("error", output.getvalue(), traceback.format_exc()[-limit:])
    try:
        connection.send(result)
    finally:
        connection.close()


def run_code(
    code: str,
    registry: ToolRegistry,
    *,
    approved: bool = False,
    timeout: float = 5.0,
    max_output_chars: int = 20_000,
) -> RunResult:
    """approved 仅由人工确认后的宿主程序设置，不能交由模型自行决定。


    不要将本函数注册为生成代码内部可调用的工具。
    超时时仅终止直接工作进程，丢弃未完成输出；不管理衍生进程。
    """
    if not approved:
        raise PermissionError("请先检查生成代码，确认后设置 approved=True")
    if timeout <= 0 or max_output_chars <= 0:
        raise ValueError("超时和输出上限必须大于零")
    if "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("当前原型需要支持 fork 的系统")
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_execute,
        args=(code, registry.namespace(), sender, max_output_chars),
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            return RunResult("timeout", error=f"执行超过 {timeout} 秒；可能已有部分副作用")
        try:
            return receiver.recv()
        except EOFError:
            return RunResult("error", error="工作进程退出，未返回执行结果")
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(0.5)
                if process.is_alive():
                    process.kill()
            process.join()
