"""直接运行或 Debug 后，在终端输入任务。"""

from pathlib import Path

from agent import Agent
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
        task = input("请输入任务：").strip()
        if not task:
            print("未输入任务，已退出。")
            return 0
        config = ModelConfig.from_env()
        registry = ToolRegistry()
        register_file_tools(registry, root)
        agent = Agent(
            ChatClient(config), registry, confirm_code,
            max_rounds=max_rounds, code_timeout=code_timeout,
        )
        print(f"文件工具根目录：{root}")
        result = agent.run(task)
    except EOFError:
        print("\n未收到任务输入，已退出。")
        return 0
    except (ValueError, OSError) as error:
        print(f"启动失败：{error}")
        return 2
    except KeyboardInterrupt:
        print("\n用户中断任务；此前执行的修改不会自动回滚。")
        return 130
    print(f"\n状态：{result.status}，模型轮数：{result.rounds}\n")
    print(result.answer)
    return 0 if result.status == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
