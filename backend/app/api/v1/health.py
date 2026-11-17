from __future__ import annotations

import redis
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import check_database
from app.schemas.responses import success_payload
from app.services.readiness import ModelReadinessChecker

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    return success_payload({"status": "ok", "ready": None})


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    checks: dict[str, bool] = {
        "configuration": False,
        "database": False,
        "redis": False,
        "models": False,
    }
    missing = settings.missing_configuration()
    checks["configuration"] = not missing
    if settings.database_url:
        try:
            check_database(settings.database_url)
            checks["database"] = True
        except Exception:
            checks["database"] = False
    if settings.redis_url:
        client = None
        try:
            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            checks["redis"] = True
        except redis.RedisError:
            checks["redis"] = False
        finally:
            if client is not None:
                client.close()
    model_result = ModelReadinessChecker(settings).check()
    checks["models"] = model_result.ready
    is_ready = all(checks.values())
    data: dict = {
        "status": "ok" if is_ready else "unavailable",
        "ready": is_ready,
        "checks": checks,
        "model_checks": model_result.checks,
    }
    if model_result.error:
        data["model_check_error"] = model_result.error
    if missing:
        data["missing_configuration"] = missing
    return JSONResponse(status_code=200 if is_ready else 503, content=success_payload(data))
