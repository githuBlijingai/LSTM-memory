from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

from app.core.metrics import record_request
from app.core.request_context import new_request_id
from app.core.security_middleware import SecurityMiddleware

logger = structlog.get_logger(__name__)


def register_middleware(app: FastAPI) -> None:
    # 先加入安全中间件，再注册请求上下文中间件，使 413/429 也带上请求 ID 与访问日志。
    app.add_middleware(SecurityMiddleware, settings=app.state.settings)

    @app.middleware("http")
    async def request_context_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = new_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = str(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=str(request_id), route=request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            record_request(
                request.method,
                _route_label(request),
                500,
                time.perf_counter() - started,
            )
            raise
        response.headers["X-Request-ID"] = str(request_id)
        latency = time.perf_counter() - started
        record_request(request.method, _route_label(request), response.status_code, latency)
        logger.info(
            "request_completed",
            method=request.method,
            status_code=response.status_code,
            latency_ms=round(latency * 1000, 3),
        )
        return response


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "unmatched"
