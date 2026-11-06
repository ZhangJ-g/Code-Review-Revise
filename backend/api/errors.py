"""Web 适配层的可公开错误，不携带接口凭据或历史正文。"""


class ServiceError(Exception):
    def __init__(self, message: str, *, code: str = "conflict", status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "status": self.status}
