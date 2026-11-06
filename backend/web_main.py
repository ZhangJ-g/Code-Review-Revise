"""支持直接运行本文件或通过 python -m backend.web_main 启动 Web 工作台。"""

# 点击“运行文件”以及 spawn 加载入口时，都从文件位置恢复包信息。
if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "backend"


def main() -> int:
    try:
        from .api.app import create_app
        from .api.service import AgentService
        from .api.errors import ServiceError
    except ModuleNotFoundError as error:
        if error.name not in {"flask", "werkzeug"}:
            raise
        print("请先用项目虚拟环境安装 requirements.txt 中的 Web 依赖。")
        return 2
    try:
        with AgentService() as service:
            app = create_app(service=service)
            print("代码检查助手 · Agent 工作台：http://127.0.0.1:8888")
            if not service.snapshot()["execution_connected"]:
                print(service.snapshot()["message"] or "执行服务未连接，当前仅可浏览。")
            else:
                print("已连接独立 Agent 进程；关闭 Web 后可通过 python -m backend.main 使用 CLI。")
            app.run(host="0.0.0.0", port=8888, debug=False, use_reloader=False, load_dotenv=False)
    except ServiceError as error:
        print(f"Web 执行服务启动失败：{error}")
        return 2
    except OSError:
        print("Web 启动失败，请检查本机 8888 端口是否已被占用。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
