"""直接运行或 Debug，选择对话和分支后在终端输入任务。"""

from pathlib import Path

from agent import Agent
from cli import run_cli
from llm import ChatClient, ModelConfig
from tools import ToolRegistry, register_file_tools


def confirm_code(code: str) -> bool:
    print("\n模型申请执行以下 Python 代码：\n")
    print(code)
    try:
        return input("\n确认执行？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def main() -> int:
    root = Path(__file__).resolve().parent
    max_rounds = 10
    code_timeout = 5.0
    try:
        config = ModelConfig.from_env()
        registry = ToolRegistry()
        register_file_tools(registry, root)
        agent = Agent(
            ChatClient(config), registry, confirm_code,
            max_rounds=max_rounds, code_timeout=code_timeout,
        )
        print(f"文件工具根目录：{root}")
        return run_cli(agent)
    except EOFError:
        print("\n输入结束，已退出。")
        return 0
    except (ValueError, OSError) as error:
        print(f"启动失败：{error}")
        return 2
    except KeyboardInterrupt:
        print("\n用户中断任务；此前执行的修改不会自动回滚。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
