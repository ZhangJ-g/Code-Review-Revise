"""本机 HTTP 入口：查询历史，向独立服务提交受校验的任务与审批命令。"""

import hashlib
import re
import secrets

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException

from ..context_manager import ContextConfig
from ..memory_store import JsonMemoryStore, PersistentMemoryStore
from ..persistent_memory_management import PersistentMemoryManager
from ..paths import ENV_FILE, FRONTEND_DIR
from .errors import ServiceError
from .serializers import conversation_dict, conversation_snapshot, fingerprint
from .memories import register_memory_routes


def create_app(*, store: JsonMemoryStore | None = None, context_config: ContextConfig | None = None,
               service=None, memory_manager=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(TRUSTED_HOSTS=["127.0.0.1", "localhost","192.168.1.216" ,"[::1]"], MAX_CONTENT_LENGTH=256 * 1024,
                      SECRET_KEY=secrets.token_bytes(32), SESSION_COOKIE_NAME="code_agent_session",
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    app.json.ensure_ascii = False
    memory = store if store is not None else JsonMemoryStore()
    # 路由和父进程服务共用一个记忆管理器；没有传入时，在应用启动时创建。
    memories = memory_manager if memory_manager is not None else getattr(service, "memory_manager", None)
    if memories is None:
        memories = PersistentMemoryManager(PersistentMemoryStore(memory.directory))
    app.extensions["persistent_memory"] = memories
    if service is not None:
        service.memory_manager = memories
    config_error = None
    if context_config is None:
        load_dotenv(ENV_FILE, override=False)
        try:
            context_config = ContextConfig.from_env()
        except ValueError as error:
            config_error = str(error)  # 只包含上下文变量名，不读取或返回模型认证配置。

    @app.before_request
    def local_request():
        origin = request.headers.get("Origin")
        if origin is not None and origin != request.host_url.rstrip("/"):
            abort(403)
        opening_page = (request.path == "/" and request.method in {"GET", "HEAD"}
                        and request.headers.get("Sec-Fetch-Mode") == "navigate"
                        and request.headers.get("Sec-Fetch-Dest") == "document")
        if request.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"} and not opening_page:
            abort(403)
        if request.method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE"}:
            abort(405)
        if request.method == "OPTIONS":
            return "", 204
        if request.method in {"POST", "PUT", "DELETE"}:
            if service is None:
                abort(405)
            csrf = session.get("csrf_token")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not csrf or not supplied.isascii() or not secrets.compare_digest(csrf, supplied):
                raise ServiceError("会话校验已失效，请刷新页面后重试。", code="csrf", status=403)
            if not request.is_json:
                abort(415)

    @app.after_request
    def response_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def api_json(data):
        response = jsonify(data)
        response.set_etag(hashlib.sha256(response.get_data()).hexdigest())
        return response.make_conditional(request)

    @app.get("/")
    def index():
        response = send_from_directory(FRONTEND_DIR, "index.html")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/assets/<name>")
    def asset(name):
        if name not in {"app.js", "memory-page.js", "styles.css", "icons.svg", "favicon.svg"}:
            abort(404)
        return send_from_directory(FRONTEND_DIR, name)

    @app.get("/api/info")
    def info():
        runtime = service.snapshot() if service is not None else {"execution_connected": False, "busy": False}
        connected = runtime["execution_connected"]
        active_memory = memories.get_active_memory()
        return api_json({"mode": "interactive" if connected else "read_only", "execution_connected": connected,
                         "capabilities": {"browse_history": True, "browse_branches": True,
                                          "submit_task": connected, "approve_tool": connected,
                                          "create_conversation": connected}, "runtime": runtime,
                         "active_memory_id": active_memory.id if active_memory else None,
                         "context_configured": context_config is not None,
                         "config_error": config_error})

    @app.get("/api/session")
    def browser_session():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify(csrf_token=session["csrf_token"])

    @app.get("/api/conversations")
    def conversations():
        items, notices = memory.list_conversations()
        return api_json({"conversations": [conversation_dict(item) for item in items], "notices": notices})

    @app.get("/api/conversations/<conversation_id>")
    def conversation(conversation_id):
        try:
            memory.path_for(conversation_id)  # 复用现有 ID / 路径校验。
        except ValueError:
            return jsonify(error={"code": "invalid_id", "message": "对话 ID 格式无效。"}), 400
        try:
            history = memory.load(conversation_id)
        except FileNotFoundError:
            abort(404)
        except (ValueError, OSError):
            return jsonify(error={"code": "unreadable_history", "message": "对话无法读取或校验失败，请在终端核对原文件。"}), 422
        leaf_id = request.args.get("leaf_id")
        # 本次页面响应固定用这一份记忆，让名称、版本和 Token 估算保持一致。
        memory_snapshot = memories.get_snapshot()
        try:
            data = conversation_snapshot(history, leaf_id=leaf_id, config=context_config, config_error=config_error,
                                         memory_snapshot=memory_snapshot)
        except ValueError:
            return jsonify(error={"code": "invalid_branch", "message": "分支不存在或不是叶节点，请刷新列表。"}), 400
        if service is not None:
            live = service.details(conversation_id)
            # 文件读取与运行事件并非同一时刻：避免把旧消息配上新的“已空闲”状态。
            for _ in range(2):
                runtime = live["runtime"]
                if (runtime.get("conversation_id") != conversation_id
                        or not runtime.get("history_revision")
                        or runtime["history_revision"] == data["history_revision"]):
                    break
                try:
                    history = memory.load(conversation_id)
                    data = conversation_snapshot(history, leaf_id=leaf_id, config=context_config, config_error=config_error,
                                                 memory_snapshot=memory_snapshot)
                except FileNotFoundError:
                    abort(404)
                except ValueError:
                    return jsonify(error={"code": "invalid_branch", "message": "分支已更新，请刷新后重新选择。"}), 400
                except OSError:
                    return jsonify(error={"code": "unreadable_history", "message": "读取历史失败，请重新加载。"}), 422
                live = service.details(conversation_id)
            data["runtime"] = live["runtime"]
            if (data["runtime"].get("conversation_id") == conversation_id
                    and data["runtime"].get("history_revision")
                    and data["runtime"]["history_revision"] != data["history_revision"]):
                data["runtime"].update(busy=True, phase="syncing", snapshot_updating=True)
            approval = live["pending_approval"]
            data["pending_approval"] = approval if (approval is not None
                and live["runtime"]["execution_connected"]
                and approval["checkpoint_node_id"] == data["selected_leaf_id"]
                and approval["history_revision"] == data["history_revision"]) else None
            observation = live["observation"]
            if observation and any(item["id"] == observation["task_id"] for item in data["tasks"]):
                data["observation"] = observation
                report = observation.get("latest_request")
                data["metrics"]["latest_request"] = report
                if report is not None:
                    data["metrics"]["usage"] = report.get("usage")
                    data["metrics"]["usage_note"] = report.get("usage_error") or "对应运行期最近一次模型请求，不与当前上下文估算相加。"
                    for record in data["compactions"]:
                        record["used_by_last_request"] = record["id"] == report["compaction_id"]
            data.pop("revision", None)
            data["revision"] = fingerprint(data)
        return api_json(data)

    def body(required, optional=()):
        data = request.get_json()
        required = set(required) | {"client_request_id"}
        if not isinstance(data, dict) or not required.issubset(data) or set(data) - required - set(optional):
            raise ServiceError("请求字段缺失或包含不支持的字段。", code="invalid_request", status=400)
        request_id = data.get("client_request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ServiceError("请求 ID 无效。", code="invalid_request", status=400)
        if "expected_revision" in required:
            version = data["expected_revision"]
            if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
                raise ServiceError("缺少有效历史版本，请刷新页面。", code="invalid_request", status=400)
        for key in ("leaf_id", "approval_id", "tool_call_id", "parent_id"):
            if key in data and data[key] is not None and (not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > 1024):
                raise ServiceError(f"{key} 无效。", code="invalid_request", status=400)
        for key in ("text", "note"):
            if key in data and (not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > 32000):
                raise ServiceError("任务或核对说明必须是 1～32000 个字符的文本。", code="invalid_request", status=400)
        if "confirmed_stopped" in data and data["confirmed_stopped"] is not True:
            raise ServiceError("请先确认旧程序及工具子进程已停止。", code="confirmation_required", status=400)
        return data

    def execute(method, data, conversation_id=None, task_id=None, status=200):
        if conversation_id is not None:
            try:
                memory.path_for(conversation_id)
            except ValueError:
                raise ServiceError("对话 ID 无效。", code="invalid_request", status=400) from None
            data["conversation_id"] = conversation_id
        if task_id is not None:
            data["task_id"] = task_id
        return jsonify(service.call(method, data)), status

    @app.post("/api/conversations")
    def create_conversation():
        return execute("create", body(()), status=201)

    @app.post("/api/conversations/<conversation_id>/branches/select")
    def select_branch(conversation_id):
        return execute("select", body(("leaf_id", "expected_revision")), conversation_id)

    @app.post("/api/conversations/<conversation_id>/tasks")
    def submit_task(conversation_id):
        return execute("submit", body(("text", "expected_revision"), ("parent_id",)), conversation_id, status=202)

    @app.post("/api/conversations/<conversation_id>/tasks/<task_id>/approval")
    def decide_tool(conversation_id, task_id):
        data = body(("approval_id", "tool_call_id", "decision", "expected_revision"))
        if not isinstance(data["decision"], str) or data["decision"] not in {"approve", "reject"}:
            raise ServiceError("审批决定无效。", code="invalid_request", status=400)
        return execute("approval", data, conversation_id, task_id, status=202)

    @app.post("/api/conversations/<conversation_id>/tasks/<task_id>/resume")
    def resume_task(conversation_id, task_id):
        return execute("resume", body(("expected_revision", "confirmed_stopped")), conversation_id, task_id, status=202)

    @app.post("/api/conversations/<conversation_id>/tasks/<task_id>/review")
    def review_task(conversation_id, task_id):
        return execute("review", body(("expected_revision", "confirmed_stopped", "note")), conversation_id, task_id)

    @app.post("/api/conversations/<conversation_id>/tasks/<task_id>/cancel")
    def cancel_task(conversation_id, task_id):
        return execute("cancel", body(("expected_revision",)), conversation_id, task_id)

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.to_dict()), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: "请求格式无效。", 403: "仅允许从本机同源页面访问。", 404: "资源或对话不存在。",
                    405: "当前 Web 入口仅支持读取历史。", 413: "请求内容过大。"}
        return jsonify(error={"code": f"http_{error.code}", "message": messages.get(error.code, "请求失败。")}), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        # 不把历史正文、环境变量或异常堆栈交给浏览器。
        return jsonify(error={"code": "internal_error", "message": "读取失败，请稍后刷新或在终端检查历史。"}), 500

    register_memory_routes(app, memories, service)
    return app
