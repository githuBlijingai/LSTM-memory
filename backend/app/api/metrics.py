from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.errors import AppError
from app.core.metrics import render_metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics")
def metrics(request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.metrics_enabled:
        raise AppError("NOT_FOUND", "接口不存在", 404)
    if settings.metrics_token is not None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        expected = settings.metrics_token.get_secret_value()
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
            raise AppError(
                "UNAUTHORIZED",
                "认证失败",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    body, content_type = render_metrics(settings)
    return Response(content=body, media_type=content_type)
