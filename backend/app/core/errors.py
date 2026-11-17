from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.responses import error_payload

logger = structlog.get_logger(__name__)


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers or {}


class UnauthorizedError(AppError):
    def __init__(self, message: str = "认证失败") -> None:
        super().__init__("UNAUTHORIZED", message, 401)


class ForbiddenError(AppError):
    def __init__(self, message: str = "没有执行此操作的权限") -> None:
        super().__init__("FORBIDDEN", message, 403)


class NotFoundError(AppError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__("NOT_FOUND", message, 404)


class IdempotencyConflictError(AppError):
    def __init__(self, message: str = "同一幂等键不能用于不同请求") -> None:
        super().__init__("IDEMPOTENCY_CONFLICT", message, 409)


class RateLimitedError(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "RATE_LIMITED",
            "请求过于频繁",
            429,
            {"retry_after_seconds": retry_after_seconds},
            headers={"Retry-After": str(retry_after_seconds)},
        )


class PayloadTooLargeError(AppError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            "PAYLOAD_TOO_LARGE",
            "请求体超过大小限制",
            413,
            {"max_bytes": max_bytes},
        )


def _response(
    request: Request, status: int, code: str, message: str, details: dict
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_payload(code, message, details),
        headers={"X-Request-ID": request.state.request_id},
    )


def _http_error(exc: HTTPException) -> tuple[str, str]:
    if exc.status_code == 401:
        return "UNAUTHORIZED", "认证失败"
    if exc.status_code == 403:
        return "FORBIDDEN", "没有执行此操作的权限"
    if exc.status_code == 404:
        return "NOT_FOUND", "接口或资源不存在"
    if exc.status_code == 405:
        return "VALIDATION_ERROR", "请求方法不允许"
    if exc.status_code == 409:
        return "CONFLICT", "请求与当前资源状态冲突"
    if exc.status_code == 429:
        return "RATE_LIMITED", "请求过于频繁"
    if exc.status_code >= 500:
        return "INTERNAL_ERROR", "服务器内部错误"
    return "VALIDATION_ERROR", str(exc.detail)


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error"),
            }
        )
    return {"errors": errors}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        response = _response(request, exc.status_code, exc.code, exc.message, exc.details)
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _response(
            request,
            422,
            "VALIDATION_ERROR",
            "请求参数校验失败",
            _validation_details(exc),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code, message = _http_error(exc)
        headers = exc.headers or {}
        headers["X-Request-ID"] = request.state.request_id
        response = _response(request, exc.status_code, code, message, {})
        response.headers.update(headers)
        return response

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_request_error", error_type=type(exc).__name__)
        return _response(request, 500, "INTERNAL_ERROR", "服务器内部错误", {})
