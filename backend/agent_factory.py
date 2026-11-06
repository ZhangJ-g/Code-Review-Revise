"""CLI/Web 共用的组装入口，不依赖 HTTP；审批和观测能力由调用方注入。"""

from typing import Callable

from .agent import Agent
from .compaction_manager import CompactionManager
from .context_manager import ContextConfig, ContextManager
from .llm import ChatClient, ModelConfig
from .memory_store import JsonMemoryStore
from .model_api import RetryPolicy
from .paths import PROJECT_ROOT
from .tools import ToolRegistry, register_file_tools


def create_registry(root=PROJECT_ROOT) -> ToolRegistry:
    registry = ToolRegistry()
    register_file_tools(registry, root)
    return registry


class _ObservedSummarizer:
    def __init__(self, client, observer) -> None:
        self.client, self.observer = client, observer

    def emit(self, name: str) -> None:
        if self.observer is not None:
            try:
                self.observer(name, {})
            except Exception:
                pass  # 展示事件不能改变摘要或任务提交结果。

    def summarize(self, messages, *, max_output_tokens: int) -> str:
        self.emit("compaction_started")
        try:
            summary = self.client.summarize(messages, max_output_tokens=max_output_tokens)
        except BaseException:
            self.emit("compaction_failed")
            raise
        self.emit("compaction_generated")
        return summary


def create_agent(
    approve: Callable[[str], bool], *, store: JsonMemoryStore | None = None,
    root=PROJECT_ROOT, observer=None, max_rounds: int = 10, code_timeout: float = 5.0,
) -> Agent:
    config = ModelConfig.from_env()
    context_config = ContextConfig.from_env()
    memory = store if store is not None else JsonMemoryStore()
    retry_policy = RetryPolicy()
    client = ChatClient(config, max_output_tokens=context_config.max_output_tokens, retry_policy=retry_policy)
    #!!!!!
    compactor = CompactionManager(
        _ObservedSummarizer(client, observer), memory, max_input_tokens=context_config.summary_input_budget,
        max_summary_tokens=context_config.summary_output_limit, tokens_per_char=context_config.tokens_per_char,
    )
    context = ContextManager(context_config, compactor, client.tool_definitions)
    return Agent(client, create_registry(root), approve, store=memory, context_manager=context,
                 retry_policy=retry_policy, max_rounds=max_rounds, code_timeout=code_timeout)
