"""Public errors never include raw provider bodies or configuration values."""
from typing import Any


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 503,
                 trace: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.trace = trace

    def body(self, request_id: str) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "request_id": request_id}}


def rebuild() -> AppError:
    return AppError("INDEX_REBUILD_REQUIRED", "索引损坏、语料变化或配置不兼容，需要重建索引")
