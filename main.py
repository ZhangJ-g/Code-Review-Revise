"""直接运行或 Debug，选择对话和分支后在终端输入任务。"""

from pathlib import Path

from agent import Agent
from cli import run_cli
from compaction import CompactionManager
from context_manager import ContextConfig, ContextManager
from llm import ChatClient, ModelConfig
from memory_store import JsonMemoryStore
from model_api import ModelError, RetryPolicy
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
        context_config = ContextConfig.from_env()
        store = JsonMemoryStore()
        retry_policy = RetryPolicy()
        client = ChatClient(config, max_output_tokens=context_config.max_output_tokens, retry_policy=retry_policy)
        compactor = CompactionManager(#独立压缩管理：累计摘要、分支适用性和临时上下文视图。
            client, store, max_input_tokens=context_config.summary_input_budget,
            max_summary_tokens=context_config.summary_output_limit, tokens_per_char=context_config.tokens_per_char,
        )
        context = ContextManager(context_config, compactor, client.tool_definitions)#请求上下文预算和压缩协调
        registry = ToolRegistry()
        register_file_tools(registry, root)
        agent = Agent(
            client, registry, confirm_code, store=store, context_manager=context, retry_policy=retry_policy,
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
    except ModelError as error:
        print(f"模型请求已停止：{error}；任务检查点已保留。")
        return 2
    except KeyboardInterrupt:
        print("\n用户中断任务；此前执行的修改不会自动回滚。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
