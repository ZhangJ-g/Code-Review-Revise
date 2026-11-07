"""支持直接运行本文件或通过 python -m backend.main 启动终端交互。"""

# 直接运行文件时没有包信息；补齐后仍使用同一套包内相对导入。
# Web 的 spawn 子进程以 __mp_main__ 加载入口时也需要这段初始化。
if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "backend"

from .agent_factory import create_agent
from .cli import run_cli
from .model_api import ModelError
from .paths import PROJECT_ROOT
from .runtime_lock import ProjectBusyError, ProjectLock
from .persistent_memory_management import PersistentMemoryManager


def confirm_code(code: str) -> bool:
    print("\n模型申请执行以下 Python 代码：\n")
    print(code)
    try:
        return input("\n确认执行？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def main() -> int:
    root = PROJECT_ROOT
    max_rounds = 10
    code_timeout = 5.0
    try:
        with ProjectLock(owner="CLI"):
            # 启动时加载记忆文件，后续模型请求直接使用这个对象的内存数据。
            persistent_manager= PersistentMemoryManager()
            agent = create_agent(confirm_code, root=root, max_rounds=max_rounds, code_timeout=code_timeout,
                                 memory_manager=persistent_manager)
            print(f"文件工具根目录：{root}")
            return run_cli(agent)
    except ProjectBusyError as error:
        print(str(error))
        return 2
    except EOFError:
        print("\n输入结束，已退出。")
        return 0
    except (ValueError, OSError) as error:
        print(f"启动失败：{error}")
        return 2
    except ModelError as error:
        print(f"模型请求已停止：{error}；任务检查点已保留。")
        return 2
    except KeyboardInterrupt:
        print("\n用户中断任务；此前执行的修改不会自动回滚。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
