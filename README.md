# 代码检查修复助手

使用 Python 3.10+，通过 `python-dotenv` 加载项目目录的 `.env`。
当前执行器需要 Linux 等支持 `fork` 的系统。

## Python 环境与 Debug

使用项目 `.venv` 的解释器安装依赖和运行，避免系统 Python 与虚拟环境混用：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

直接运行或 Debug `main.py`，在终端出现“请输入任务：”后输入自己的任务，无需配置任务参数。
VS Code 中也可以选择“Debug 代码检查助手（项目虚拟环境）”配置，按 F5 后在终端输入任务。
该配置显式使用 `.venv/bin/python`，并使用集成终端接收代码执行确认。
普通运行时也应通过“Python: Select Interpreter”选择项目虚拟环境。
PyCharm 中将项目及运行配置的解释器设为项目 `.venv/bin/python`，运行后在控制台输入任务。

## 启动 Agent

接口地址可填完整的 POST 地址；若只填域名或以 `/v1` 结尾的基础地址，程序会自动补成
`/v1/chat/completions`。其他自定义路径保持原样。当前协议为 Chat Completions
兼容格式，服务需支持 `tools` 函数调用；仅修改 URL 不能适配完全不同的请求协议。

```bash
export MODEL_API_URL='https://your-provider.example/v1/chat/completions'
export MODEL_NAME='your-model'
# 交互读取密钥，避免将真实密钥写入 shell 历史或源码。
read -rsp 'API Key: ' MODEL_API_KEY
export MODEL_API_KEY

.venv/bin/python main.py
```

配置项：

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `MODEL_API_URL` | 完整接口地址或常见基础地址 | 必填 |
| `MODEL_NAME` | 模型名称 | 必填 |
| `MODEL_API_KEY` | 认证值；本地免认证服务可不设置 | 空 |
| `MODEL_AUTH_HEADER` | 认证请求头名称 | `Authorization` |
| `MODEL_AUTH_PREFIX` | 认证值前缀，注意末尾空格 | `Bearer ` |
| `MODEL_EXTRA_HEADERS` | 额外请求头，JSON 字符串 | `{}` |
| `MODEL_TIMEOUT` | 单次 API 请求超时秒数 | `60` |

例如服务使用 `api-key: <密钥>` 而非 Bearer 认证：

```bash
export MODEL_AUTH_HEADER='api-key'
export MODEL_AUTH_PREFIX=''
```

其他 token 也可以放在 `MODEL_API_KEY` 中，通过上述两个配置控制发送方式。
多个认证字段可用 `MODEL_EXTRA_HEADERS` 提供；与认证字段重名时，以单独配置的认证值为准。
程序自动加载与 `llm.py` 同目录的 `.env`，不依赖当前工作目录；已存在的进程环境变量优先，
不会被 `.env` 覆盖。不要将包含真实密钥的 `.env` 提交到版本库。
服务若有不同的消息或工具调用协议，应修改 `llm.py` 的适配逻辑。

每段模型生成代码都会展示并询问确认，只有输入 `y` 才执行。
运行配置直接在 `main.py` 的 `main()` 中修改：`root` 是文件工具根目录，
默认为项目目录；`max_rounds = 10` 控制最大模型调用次数，
`code_timeout = 5.0` 控制每次代码执行超时。不再解析命令行参数。
达到上限、拒绝执行或 API 失败时以未完成状态退出，不会自动重放已执行代码。

## 执行循环

`main.py` 组装工具与接口 → `agent.py` 建立系统、用户历史节点 → `llm.py` 请求模型。
模型调用 `run_code` 时，宿主先确认代码，再调用 `runner.py`，将状态、输出和异常
记录为带 `tool_call_id` 的工具消息，连同历史发送到下一轮。
模型返回没有工具调用的普通回答时结束，最终回答应包含原代码问题、修改和验证依据。

每条消息使用现有 `Session` 结构建立父子关系，`Conversation` 保存对话 ID、根节点、
当前叶节点及创建/更新时间。`agent.history_tree` 保留整棵历史树，`agent.history`
仅返回当前分支。每次 `agent.run()` 默认新建对话，也可以传入已有历史继续。
长任务可能超过模型上下文窗口。当前非流式，不展示模型内部思考过程。
“完成”表示模型已给出最终回答，不代表独立评估已证明所有修复正确。
程序不会自动运行项目里的 `error.py`，也不会在启动时修改它。

## 历史持久化与分支

`history.py` 管理节点与分支，`memory_store.py` 负责 JSON 编解码和文件读写。
默认保存位置是项目目录下的 `memory/<conversation_id>.json`，不依赖启动工作目录。
每个文件包含 `schema_version`、`conversation` 元数据及全部 `nodes`。
时间保存为带时区的 ISO 8601 字符串，角色保存为字符串，加载时恢复为对应 Python 类型。

Agent 追加用户消息、助手消息或工具结果后都会保存一次。保存前和加载后校验：
ID 唯一、唯一根节点、双向父子关系、全树连通无环，以及当前节点确实为叶节点。
使用临时文件写入、flush/fsync 后原子替换，写盘失败时停止当前执行。

当前仅支持单进程写入；没有并发锁、自动备份或断电后目录元数据持久化保证。

以下示例使用你已创建的 `agent`，不改变 `main.py` 的交互：

```python
# 新建对话：自动保存。
result = agent.run(task)
conversation_id = agent.history_tree.conversation.id

# 继续当前分支：也可将 loaded 传给另一个新创建的 Agent。
loaded = agent.store.load(conversation_id)
result = agent.run(next_task, history=loaded)

# 从历史中的完整节点创建新分支，旧分支保留。
result = agent.run(branch_task, history=loaded, parent_id=earlier_node_id)

# 切换回已有分支并保存选择。
loaded.select_leaf(other_leaf_id)
agent.store.save(loaded)
```

`History.append(message, parent_id=...)` 也可以直接创建分支。
当前上下文通过根到当前节点的祖先链构造，不会混入其他分支。
再次调用 `agent.run()` 时必须显式传 `history` 才会继续，不会自动加载最近对话。
`main.py` 仍然每次新建对话，暂未添加选择旧对话的菜单。

恢复时沿用保存的系统提示词，因此应提供与原对话相符的工具配置。
若当前分支存在未配对的工具调用与结果，将拒绝继续，不会自动重放。
工具已产生副作用、但结果尚未写入时也可能出现这种状态，需核对实际文件，
或明确从更早的完整节点创建分支。创建分支只改变聊天历史，不会恢复或回滚项目文件。
历史保存完整消息和工具结果，尚未实现脱敏、加密、摘要或上下文压缩。

## 最小用法

```python
from tools import ToolRegistry, register_file_tools
from prompts import build_system_message
from runner import run_code

registry = ToolRegistry()
register_file_tools(registry, ".")
system_message = build_system_message(registry)
# 接入模型后，将 system_message 和历史消息一同发送给模型。
print(system_message.content)

# 当前手工模拟模型生成的代码；真实接入后也应先展示代码供人工检查。
code = 'text = read_file("models.py")\nprint(text[:500])'
print(code)
# 仅在人工确认这段具体代码之后，由宿主程序设置 approved=True。
result = run_code(code, registry, approved=True)
print(result.status, result.output, result.error)
```

`approved` 是调用方对人工确认的声明，不是执行器自动完成审批。
不要允许模型设置它，也不要把 `run_code` 注入生成代码的工具列表。

## 工具组织

- `tools/base.py`：注册名称、函数和一句描述，自动提取参数及返回值签名。
- `tools/files.py`：读取、查看目录、写入；默认禁止覆盖，父目录需已存在。
- `tools/search.py`：注入同步搜索函数，真实服务由后续选择和配置。
- `tools/mcp.py`：注册已连接 MCP 工具的同步适配函数，参数签名需真实反映工具参数。
- `prompts.py`：只把已注册工具放入系统消息；注册变化后需重新构建消息。
- `runner.py`：独立进程执行，返回 `success`、`error` 或 `timeout`。

搜索和 MCP 当前只有接入入口，没有真实服务连接。调用方负责认证、客户端生命周期、
外部请求超时，以及需要时把异步接口包装为同步函数。不要直接继承正在使用的网络连接；
此 fork 原型的适配函数应在子进程中创建自己的客户端。

## 执行边界

这是单线程、可信本地实验原型，不是安全沙箱。生成的 Python 具有当前用户权限，
可以绕过文件工具访问文件和网络。独立进程只隔离 Python 变量状态，不隔离系统权限。
父进程内存中的工具状态变化不会被子进程同步回来；文件等外部修改会保留。

文件工具拒绝解析后越出项目目录的路径，但不防御并发替换符号链接等竞态。
执行器截取 Python 标准输出和异常；底层文件描述符写入和衍生进程输出不在保证范围内。
超时终止直接工作进程，可能丢失此前输出，也不终止其衍生进程。
异常或超时不会回滚已写文件及外部副作用。不要对不可信代码使用此执行器。

`llm.py` 仅向模型 API 声明 `run_code` 一个工具；文件、搜索及 MCP 的签名通过
系统提示词提供，模型在生成的 Python 中组合调用它们。宿主的 `approved` 参数
不属于模型可传参数，不允许模型自行批准执行。

## 验证

```bash
.venv/bin/python -m unittest -v test_tools test_agent test_memory
```

测试使用临时目录、搜索/MCP 替身和模拟 HTTP 响应，不修改项目源文件，不访问外部服务。
包含 API 请求序列化、认证配置、工具反馈、文件修改闭环、取消、轮数上限和异常反馈。
历史测试覆盖创建分支、保存、重新加载、继续对话、上下文分支隔离、无效历史校验，
以及工具执行前后保存失败时的行为。
