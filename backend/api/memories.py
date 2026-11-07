"""处理前端的记忆管理请求；Agent 正在执行或等待审批时也能修改记忆。"""

import re

from flask import jsonify, request

from ..memory_store import MemoryConflict
from .errors import ServiceError


def register_memory_routes(app, manager, service) -> None:
    """注册列表、新建、修改、删除和应用接口，统一使用传入的记忆管理器。"""
    def writable():
        return service is not None and service.snapshot()["execution_connected"]

    @app.get("/api/memories")
    def memories():
        return jsonify(**manager.list_memories(), writable=writable())

    def change(operation, memory_id=None):
        """检查前端参数，交给管理器处理，再把结果或错误返回页面。"""
        if not writable():
            raise ServiceError("当前服务仅可浏览记忆，请使用持有项目写入锁的 Web 入口。", code="read_only", status=405)
        data = request.get_json()
        fields = {"client_request_id", "expected_memory_revision"}
        if operation in {"create", "update"}:
            fields |= {"name", "content"}
        if operation == "apply":
            fields.add("memory_id")
        if not isinstance(data, dict) or set(data) != fields:
            raise ServiceError("记忆请求字段缺失或包含不支持的字段。", code="invalid_request", status=400)
        # 请求 ID 防止重复操作；版本号防止用旧编辑覆盖其他人的新修改。
        request_id, revision = data["client_request_id"], data["expected_memory_revision"]
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ServiceError("请求 ID 无效。", code="invalid_request", status=400)
        if type(revision) is not int or revision < 0:
            raise ServiceError("记忆修订号无效，请刷新列表。", code="invalid_request", status=400)
        if operation == "apply":
            memory_id = data["memory_id"]
        if memory_id is not None and (not isinstance(memory_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", memory_id)):
            raise ServiceError("记忆 ID 无效。", code="invalid_request", status=400)
        try:
            result = manager.execute(operation, memory_id=memory_id, name=data.get("name"),
                                     content=data.get("content"), expected_revision=revision, request_id=request_id)
        except MemoryConflict as error:
            raise ServiceError(str(error), code="memory_conflict", status=409) from None
        except LookupError:
            raise ServiceError("记忆不存在，请刷新列表。", code="not_found", status=404) from None
        except ValueError as error:
            raise ServiceError(str(error), code="invalid_request", status=400) from None
        except OSError:
            raise ServiceError("记忆保存失败，原有记忆与应用状态已保留。", code="storage_error", status=503) from None
        return jsonify(result), 201 if operation == "create" else 200

    app.add_url_rule("/api/memories", "create_memory", lambda: change("create"), methods=["POST"])
    app.add_url_rule("/api/memories/<memory_id>", "update_memory",
                     lambda memory_id: change("update", memory_id), methods=["PUT"])
    app.add_url_rule("/api/memories/<memory_id>", "delete_memory",
                     lambda memory_id: change("delete", memory_id), methods=["DELETE"])
    app.add_url_rule("/api/memories/active", "apply_memory", lambda: change("apply"), methods=["POST"])
