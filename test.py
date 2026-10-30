#```python
from tools import ToolRegistry, register_file_tools
from prompts import build_system_message
from runner import run_code

if __name__ == "__main__":
    registry = ToolRegistry()# 注册工具对象
    register_file_tools(registry, ".")#传入对象进行工具注册，让registry有工具的属性
    system_message = build_system_message(registry)
    # 接入模型后，将 system_message 和历史消息一同发送给模型。
    print(system_message.content)

    # 当前手工模拟模型生成的代码；真实接入后也应先展示代码供人工检查。
    code = 'text = read_file("./error.py")\nprint(text[:500])'
    print(code)
    # 仅在人工确认这段具体代码之后，由宿主程序设置 approved=True。
    result = run_code(code, registry, approved=True)# 修改代码
    print(result.status, result.output, result.error)

    # 当前手工模拟模型生成的代码；真实接入后也应先展示代码供人工检查。
    code = 'text = read_file("./error.py")\nprint(text[:500])'
    print(code)
    # 仅在人工确认这段具体代码之后，由宿主程序设置 approved=True。
    result = run_code(code, registry, approved=True)
    print(result.status, result.output, result.error)