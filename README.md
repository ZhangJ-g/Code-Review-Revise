# 代码检查修复助手

使用 Python 3.10+，通过 `python-dotenv` 加载项目目录的 `.env`。
当前执行器需要 Linux 等支持 `fork` 的系统。

## Python 环境与 Debug

使用项目 `.venv` 的解释器安装依赖和运行，避免系统 Python 与虚拟环境混用：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

直接运行或 Debug `main.py`，在终端选择对话和分支后输入自己的任务，无需配置任务参数。
VS Code 中也可以选择“Debug 代码检查助手（项目虚拟环境）”配置，按 F5 后操作终端菜单。
该配置显式使用 `.venv/bin/python`，并使用集成终端接收代码执行确认。
普通运行时也应通过“Python: Select Interpreter”选择项目虚拟环境。
PyCharm 中将项目及运行配置的解释器设为项目 `.venv/bin/python`，运行后在控制台选择对话并输入任务。

终端入口自动启用标准库 `readline` 的行编辑，处理中文退格和光标移动；不额外保存输入历史。
Debug 时使用集成终端，IDE 的调试控制台未必支持同样的终端编辑行为。

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
达到上限或 API 失败时，任务保留检查点，终端可选择恢复、明确结束或返回列表；拒绝代码执行会取消该任务。不会自动重放已执行代码。

## 终端对话和分支菜单

```text
启动 → 我的对话 → 新建或加载 → 选择分支
                               ↓
                      回车沿用当前 / 编号切换
                               ↓
                       输入任务 → Agent.run
                               ↓
                       保存 → 继续输入或返回
```

`cli.py` 负责菜单和连续输入，不调用模型选择对话或分支：

- 扫描 `memory/*.json`，校验后按更新时间倒序展示标题和更新时间，时间按北京时间显示。
- 新对话首次输入任务后保存，标题取首条用户任务的简短摘要，不额外调用模型生成标题。
- 旧文件没有 title 字段时，从首条用户消息恢复标题，读取操作本身不覆盖旧文件。
- 选择旧对话后按 Conversation.id 重新加载文件，默认分支来自保存的 current_leaf_id。
- 分支列表每项对应一个叶节点，展示其路径中的最后一条用户消息、最后一条助手消息和最后消息时间；“当前”表示已选分支。
- 回车沿用当前分支，编号选择其他叶节点后立即保存，因此下次打开仍沿用该选择。
- 有任务检查点的未结束分支标记为“任务待恢复”或“结果待核对”，打开后显式选择恢复。没有检查点、消息又不完整的旧分支仍标记为“暂不可继续”。
- 读取失败或校验失败的历史文件单独提示并跳过，不影响其他对话。

| 输入位置 | 输入 | 行为 |
| --- | --- | --- |
| 对话列表 | 对应编号 | 加载旧对话或创建新对话 |
| 对话列表 | `0` | 退出程序 |
| 分支列表 | 回车 | 沿用当前分支 |
| 分支列表 | 对应编号 | 切换并保存叶节点选择 |
| 分支列表 | `0` | 返回对话列表 |
| 任务输入 | 普通文本 | 提交任务，随后可连续输入下一条 |
| 任务输入 | `/fork` | 选择当前分支中的旧消息，在它后面继续 |
| 任务输入 | `/branches` | 打开当前对话的分支列表 |
| 任务输入 | `/back` | 返回对话列表 |
| 任务输入 | `/quit` | 退出程序 |

恢复的模型上下文仍为“保存的系统消息 → 当前分支历史 → 新任务”，不会把其他分支拼到系统提示词中。
使用 `/fork` 可从当前分支中的旧节点创建新分支；需要从其他分支的消息开始时，先通过 `/branches` 切换到该分支。
当前只实现终端交互，未引入 GUI、新框架或新依赖。

### 从旧消息创建分支

1. 在任务输入处输入 `/fork`。
2. 按编号选择当前分支的一条消息，列表显示角色和摘要；工具调用尚未完整配对的位置标记为不可继续。
3. 查看选中消息的完整内容，确认新任务要接在它后面。
4. 输入新任务，程序调用 `agent.run(task, history=history, parent_id=selected_id)`。
5. 新用户节点的 parent_id 指向选中节点，current_leaf_id 更新为新节点；保存后请求模型，后续消息沿新路径继续。

选择菜单输入 `0` 或回车可取消；预览后留空或输入 `/cancel` 也可取消。取消不创建节点，不改变当前叶节点，不写入文件，也不调用模型。
选中已有后续消息的旧节点会创建分叉，并保留原分支；选中当前叶节点只是普通追加。
这项操作是在选中消息后继续，不会修改选中消息或替换它原有的回答。

创建分支前不会把 current_leaf_id 改成中间节点，只有提交有效任务后才由 History.append() 更新为新节点 ID。
模型收到的是“根 → 选中消息 → 新任务”的路径，原分支中位于选中消息之后的内容不会发送。
分支只改变聊天上下文，沿用当前项目文件，不恢复历史文件版本。

## 执行循环

`main.py` 组装工具与接口 → `cli.py` 选择对话、分支和接收任务 → `agent.py` 维护历史节点 → `llm.py` 请求模型。
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
每个文件包含 `schema_version`、`conversation` 元数据、全部 `nodes` 及任务检查点 `tasks`。旧文件没有 tasks 时按空列表加载，不编造历史执行阶段。
时间保存为带时区的 ISO 8601 字符串，角色保存为字符串，加载时恢复为对应 Python 类型。

Agent 追加用户消息、助手消息或工具结果后都会保存一次。保存前和加载后校验：
ID 唯一、唯一根节点、双向父子关系、全树连通无环，以及当前节点确实为叶节点。
使用临时文件写入、flush/fsync 后原子替换，写盘失败时停止当前执行。

当前仅支持单进程写入；没有并发锁、自动备份或断电后目录元数据持久化保证。

以下示例是终端菜单所使用的底层接口，假设你已创建 `agent`：

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
终端菜单在选择旧对话和连续输入时会显式传入 history；只有用户选择“创建新对话”才新建历史树。

恢复时沿用保存的系统提示词，因此应提供与原对话相符的工具配置。
普通新任务仍要求父节点前的工具调用与结果完整。已有任务通过专门的恢复入口处理待确认或结果未知的调用，不会直接重放。
没有任务检查点的旧分支若存在未配对调用，无法可靠推断工具是否开始，仍拒绝自动恢复。
创建分支只改变聊天历史，不会恢复或回滚项目文件。
历史保存完整消息和工具结果，尚未实现脱敏、加密、摘要或上下文压缩。

## 任务检查点与中断恢复

每次提交新任务时创建 `TaskRun`，通过 `user_node_id` 关联原始用户消息。
它记录独立任务 ID、最后保存的节点 `checkpoint_node_id`、状态、阶段、活动工具调用 ID、累计模型请求尝试次数、错误和时间。
同一 Conversation 可保存多个任务记录，不把整段对话简单标记为一个执行中任务。

| 状态 | 含义 |
| --- | --- |
| `running` | 任务执行中，或上次进程未正常结束；单凭该字段不能判断进程是否还活着 |
| `interrupted` | 请求失败或捕获到中断，可从阶段检查点继续 |
| `needs_review` | 工具结果不确定，先核对进程和副作用 |
| `limit` | 本次模型轮数预算用尽，可显式恢复 |
| `completed` | 最终回答和完成状态已保存，表示执行循环结束，不代表修复一定正确 |
| `cancelled` | 用户明确结束任务，已经产生的修改保留 |

检查点按以下顺序提交：

```text
用户任务 + running / waiting_model 一起保存
→ 请求模型（请求尝试次数先保存）
→ 助手工具请求 + waiting_approval 一起保存
→ 人工确认
→ executing_tool + active_tool_call_id 先保存
→ run_code
→ 工具结果 + 下一阶段一起保存
→ 继续请求模型
→ 最终回答 + completed / finished 一起保存
```

由 `Agent._checkpoint()` 统一写一份 JSON 快照，避免回答已写入却单独丢失完成标记，或完成标记先于最终回答写入。
保存失败会停止，不尝试把内存中的未提交状态再补写一次；后续必须重新加载磁盘历史。

打开待恢复分支后，终端展示原任务、阶段和累计轮数，提供“恢复原任务”“结束原任务”“返回列表”：

| 保存阶段 | 恢复行为 |
| --- | --- |
| `waiting_model` | 用现有上下文重新请求模型；上次请求可能已到达服务端，可能重复计费 |
| `waiting_approval` | 重新展示尚未执行的代码并确认，跳过已经保存结果的调用 |
| `executing_tool` | 不能断定代码是否生效，暂停并要求核对，不直接重跑 |
| `finished` | 不重复执行已结束的任务 |

超时或工作进程没有返回结果也会暂停为 needs_review，而不是直接自动重试。
结果未知时，只有用户输入“已核对”，确认旧程序及工具子进程已停止，并记录实际文件/外部操作状态，才可继续。
核对后为缺失结果的原调用追加 `TOOL_OUTCOME_UNKNOWN`，为同批尚未开始的调用追加 `TOOL_NOT_STARTED`；这些是恢复说明，不是假造成功结果。
已有超时/未知结果不重复追加。另保存一条明确标记的人工恢复核对消息，让模型依据真实状态继续分析。
之后如模型提出新代码，仍需逐段人工确认。

底层接口示例，假设 agent 已创建：

```python
loaded = agent.store.load(conversation_id)
task = loaded.unfinished_task()
if task is not None:
    result = agent.resume(loaded, task.id)
    # needs_review 会停止在人工核对边界，不自动执行未知结果的代码。
```

恢复复用原 TaskRun，不重复追加原始 task。累计轮数保留；每次显式恢复最多再使用 max_rounds 次模型请求。
有未结束任务的同一检查点不接受另一条新任务，用户可先恢复或明确结束原任务。
本阶段支持单进程使用，不提供会话并发锁，不自动探测或终止旧进程。Ctrl+C 会尽力保存状态；强杀进程无法依赖 finally，工具子进程甚至衍生进程可能仍存活，恢复前必须核对。
不能把 TaskRun 的标记当作进程已经结束的证明。

JSON 原子替换只覆盖历史快照，不能使“修改项目文件或外部服务”和“保存历史”成为同一事务；没有恰好执行一次、自动回滚或从任意 Python 语句断点继续的保证。

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
- `runner.py`：独立进程执行，返回 `success`、`error`、`timeout` 或工作进程无结果时的 `unknown`。

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

## Token 统计模块（尚未接入运行流程）

`token_stats.py` 独立提供请求前的输入估算和响应后的 usage 解析，不导入 Agent、模型客户端或历史管理模块。
它不发送 API 请求、不改变上下文，也不判断窗口阈值或触发压缩。

```python
from token_stats import estimate_request_tokens, parse_usage

# 使用实际准备发给 API 的消息字典列表，不传 History、TaskRun 或整个请求体。
messages = [{"role": "user", "content": "检查代码"}]
estimate = estimate_request_tokens(messages, tools=[])
print(estimate.estimated_input_tokens)

# 这里用样例响应演示；以后接入时传完整的 API 响应字典。
usage = parse_usage({"usage": {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
}})
print(usage.prompt_tokens)
```

- `estimate_text_tokens(text)`：默认按 Unicode 字符数 × 2 向上取整，系数 tokens_per_char 可配置。
- `estimate_request_tokens(messages, tools)`：对 messages 和工具 schema 的紧凑 JSON 表示分别估算，包含工具代码、参数及结果；返回消息、工具定义和额外开销分项。
- `extra_overhead_tokens` 默认 0，仅在调用方明确掌握额外开销时添加。序列化结构已计入，模块不编造固定的逐消息协议开销。
- `parse_usage(response)`：读取本次响应的 prompt_tokens、completion_tokens、total_tokens。缺失或 null 保留为 None，明确返回 0 才记录为 0；没有可识别用量时整体返回 None。
- 无效的计数字段（负数、布尔值、字符串等）抛 ValueError；接入时需要单独处理，不能让统计异常导致重复执行工具。

字符/JSON 估算未经模型分词校准，不是实际计费值，也不是安全上界，后续窗口控制仍需预留输出空间和误差余量。
API usage 与估算分别保存，不相加成窗口占用，不把历次 total_tokens 累加来判断本次请求大小。
统计范围由调用方决定，模块不自动选择分支，应传入已经选好的当前请求消息。
它不估计模型尚未生成的输出，也不将缓存或推理统计子项再次加到总数。

## 验证

```bash
.venv/bin/python -m unittest -v test_tools test_agent test_memory
```

测试使用临时目录、搜索/MCP 替身和模拟 HTTP 响应，不修改项目源文件，不访问外部服务。
包含 API 请求序列化、认证配置、工具反馈、文件修改闭环、取消、轮数上限和异常反馈。
历史测试覆盖创建分支、保存、重新加载、继续对话、上下文分支隔离、无效历史校验，
以及工具执行前后保存失败时的行为。
