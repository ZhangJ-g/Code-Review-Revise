"""终端对话、分支菜单和连续输入；不负责模型协议或历史序列化。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from agent import Agent
from history import History, excerpt
from models import Role
from prompts import build_system_message


def configure_terminal_input() -> None:
    """启用字符级行编辑，避免终端按字节退格导致中文残留。"""
    try:
        import readline
    except ImportError:
        # 不提供 readline 的平台仍可使用普通 input。
        return
    # 导入后所有 input() 自动使用行编辑；不额外记录用户的输入历史。
    readline.set_auto_history(False)


def display_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def display_token_report(agent: Agent) -> None:
    """服务端 usage 对应最后一次实际请求；当前视图只显示估算。"""
    if agent.request_reports:
        last = agent.request_reports[-1]
        print(f"本次模型请求尝试：{len(agent.request_reports)} 次；最后请求输入估算：{last.estimated_input_tokens} token")
        if last.usage is not None:
            usage = last.usage
            prompt = "未知" if usage.prompt_tokens is None else usage.prompt_tokens
            completion = "未知" if usage.completion_tokens is None else usage.completion_tokens
            print(f"该请求 API 用量：输入 {prompt}，输出 {completion} token")
        else:
            print("该请求 API 未提供有效 usage，使用发送前估算。")
        if last.usage_error:
            print(last.usage_error)
    view = agent.last_round_context
    if view is not None:
        budget = "未配置" if view.input_budget is None else str(view.input_budget)
        print(f"本轮结束的上下文输入估算：{view.estimate.estimated_input_tokens} token；输入预算：{budget}")
    if agent.last_round_error:
        print(agent.last_round_error)


def choose_conversation(agent: Agent) -> History | None:
    while True:
        conversations, notices = agent.store.list_conversations()
        for notice in notices:
            print(notice)
        print("\n===== 我的对话 =====")
        # 这里的 1 是 enumerate() 的第二个参数，表示从 1 开始编号。
        for index, conversation in enumerate(conversations, 1):
            print(f"{index}. {excerpt(conversation.title, 60)}  更新：{display_time(conversation.updated_at)}")
        new_number = len(conversations) + 1
        print(f"{new_number}. 创建新对话")
        print("0. 退出")
        choice = input("请选择对话：").strip()
        if choice == "0":
            return None
        if choice == str(new_number):
            # 此时只创建内存中的系统根节点，输入第一条任务后才保存。
            return History.create(build_system_message(agent.registry))
        if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(conversations):
            print("请输入列表中的编号。")
            continue
        try:
            return agent.store.load(conversations[int(choice) - 1].id)
        except (ValueError, OSError):
            print("所选对话无法加载，请重新选择；原文件未修改。")


def choose_branch(agent: Agent, history: History) -> bool:
    """True 表示选好分支；False 返回对话列表。回车保留持久化的当前选择。"""
    while True:
        branches = history.branches()
        blocked = {}
        print(f"\n===== 选择分支：{excerpt(history.conversation.title) or '新对话'} =====")
        for index, branch in enumerate(branches, 1):
            marker = "（当前）" if branch.is_current else ""
            pending_task = history.unfinished_task(branch.leaf_id)
            try:
                if pending_task is not None:
                    agent.validate_recovery(history, pending_task.id)
                    marker += " [结果待核对]" if pending_task.phase == "executing_tool" or pending_task.status == "needs_review" else " [任务待恢复]"
                else:
                    agent.validate_resume(history, parent_id=branch.leaf_id)
            except ValueError as error:
                blocked[branch.leaf_id] = str(error)
            warning = " [暂不可继续]" if branch.leaf_id in blocked else ""
            print(f"{index}. {excerpt(branch.last_user_message) or '尚未开始对话'}{marker}{warning}")
            print(f"   用户：{excerpt(branch.last_user_message, 100) or '暂无'}")
            print(f"   助手：{excerpt(branch.last_assistant_message, 100) or '暂无'}")
            print(f"   最后消息：{display_time(branch.last_message_at)}")
        print("0. 返回对话列表")
        choice = input("选择分支（回车沿用当前）：").strip()
        if choice == "0":
            return False
        if not choice:
            leaf_id = history.conversation.current_leaf_id
        elif choice.isascii() and choice.isdecimal() and 1 <= int(choice) <= len(branches):
            leaf_id = branches[int(choice) - 1].leaf_id
        else:
            print("请输入列表中的编号，或回车沿用当前分支。")
            continue
        if leaf_id in blocked:
            print(blocked[leaf_id])
            continue
        if leaf_id != history.conversation.current_leaf_id:
            old_leaf = history.conversation.current_leaf_id
            old_updated_at = history.conversation.updated_at
            history.select_leaf(leaf_id)
            try:
                agent.store.save(history)
            except OSError:
                history.conversation.current_leaf_id = old_leaf
                history.conversation.updated_at = old_updated_at
                print("分支选择保存失败，已保留原选择，请返回后检查存储目录。")
                continue
        return True


def prepare_fork(agent: Agent, history: History) -> tuple[str, str] | None:
    """只选择父节点并收集新任务；不改历史，提交由 Agent.run 统一完成。"""
    entries = history.path()
    roles = {Role.SYSTEM: "系统", Role.USER: "用户", Role.ASSISTANT: "助手", Role.TOOL: "工具"}
    choices = {str(index): entry for index, entry in enumerate(entries, 1)}
    blocked = {}
    print("\n===== 从这里继续：选择当前分支中的消息 =====")
    for number, entry in choices.items():
        try:
            agent.validate_resume(history, parent_id=entry.id)
        except ValueError as error:
            blocked[entry.id] = str(error)
        warning = " [不可从此处继续]" if entry.id in blocked else ""
        text = entry.message.content
        if not text.strip() and entry.message.tool_calls:
            text = "[工具调用：" + ", ".join(call.name for call in entry.message.tool_calls) + "]"
        print(f"{number}. {roles[entry.message.role]}：{excerpt(text, 100) or '无正文'}{warning}")
    print("0. 取消，保留当前分支")

    while True:
        choice = input("选择消息编号（回车取消）：").strip()
        if choice in {"", "0"}:
            print("已取消，当前分支未改变。")
            return None
        entry = choices.get(choice)
        if entry is None:
            print("请输入列表中的编号。")
            continue
        if entry.id in blocked:
            print(blocked[entry.id])
            continue
        print(f"\n选中的消息：{roles[entry.message.role]}，{display_time(entry.created_at)}")
        print(entry.message.content or "（无正文）")
        for call in entry.message.tool_calls:
            print(json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False, indent=2))
        if entry.children_ids:
            print("新任务将接在这条消息之后，原来的后续分支会保留。")
        else:
            print("这条消息是当前末尾，新任务将直接追加，不会产生额外分叉。")
        print("沿用当前项目文件，不恢复历史版本。")
        task = input("请输入新任务（留空或 /cancel 取消）：").strip()
        if not task or task == "/cancel":
            print("已取消，未创建节点，当前分支未改变。")
            return None
        return entry.id, task


def recover_pending_task(agent: Agent, history: History) -> bool:
    """显式恢复当前分支的原任务；False 返回列表，不新建用户任务。"""
    task = history.unfinished_task()
    if task is None:
        return True
    phases = {"waiting_model": "等待模型分析", "waiting_approval": "等待代码执行确认", "executing_tool": "工具结果不确定"}
    print("\n===== 上次任务尚未结束 =====")
    print(f"任务：{excerpt(history.nodes[task.user_node_id].message.content, 120)}")
    print(f"进度：{phases.get(task.phase, task.phase)}，累计模型请求尝试 {task.rounds} 次")
    if task.error:
        print(f"记录：{excerpt(task.error, 160)}")
    print("恢复前请确保上一次程序及工具子进程已经停止；不会重跑已有结果的工具。")
    print("1. 恢复原任务（不重复提交任务消息）")
    print("2. 结束原任务，不再继续")
    print("0. 返回对话列表")
    while True:
        choice = input("请选择：").strip()
        if choice in {"", "0"}:
            return False
        if choice in {"1", "2"}:
            break
        print("请输入 1、2 或 0。")
    if task.phase == "executing_tool" or task.status == "needs_review":
        print("工具代码可能已经部分或全部生效，也可能有子进程仍在运行，不能直接重跑。")
        confirm = input("确认旧进程已停止且已检查实际修改后，输入“已核对”；其他输入返回：").strip()
        if confirm != "已核对":
            return False
        note = input("请记录实际文件/外部操作状态（留空返回）：").strip()
        if not note:
            return False
        agent.acknowledge_unknown(history, task.id, note, confirmed_stopped=True)
    result = agent.cancel_task(history, task.id) if choice == "2" else agent.resume(history, task.id)
    print(f"\n状态：{result.status}，累计模型请求尝试：{result.rounds}")
    print(result.answer)
    display_token_report(agent)
    return True


def run_cli(agent: Agent) -> int:
    configure_terminal_input()
    while True:
        history = choose_conversation(agent)
        if history is None:
            return 0
        if not choose_branch(agent, history):
            continue
        print("\n请输入任务；/fork 从旧消息继续，/branches 切换分支，/back 返回列表，/quit 退出。")
        while True:
            if history.unfinished_task() is not None:
                try:
                    if not recover_pending_task(agent, history):
                        break
                except (ValueError, OSError) as error:
                    print(f"恢复已停止：{error}。请重新加载磁盘历史；不会回滚已有文件修改。")
                    break
                continue
            task = input("你：").strip()
            if not task:
                continue
            if task == "/quit":
                return 0
            if task == "/back":
                break
            if task == "/branches":
                if not choose_branch(agent, history):
                    break
                continue
            parent_id = None
            if task == "/fork":
                request = prepare_fork(agent, history)
                if request is None:
                    continue
                parent_id, task = request
            try:
                result = agent.run(task, history=history, parent_id=parent_id)
            except (ValueError, OSError) as error:
                print(f"当前对话已停止：{error}")
                print("返回对话列表；请以磁盘保存的状态为准，已经产生的文件修改不会回滚。")
                break
            print(f"\n状态：{result.status}，累计模型请求尝试：{result.rounds}")
            print(result.answer)
            display_token_report(agent)
