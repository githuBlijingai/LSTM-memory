from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, Header, Request

from app.core.config import Settings
from app.core.errors import AppError
from app.db import get_session_factory
from app.ml.registry import ModelBundle, get_model_registry
from app.services.uow import UnitOfWork


def get_settings_dependency(request: Request) -> Settings:
    return request.app.state.settings


def get_model_bundle(settings: Settings = Depends(get_settings_dependency)) -> ModelBundle:
    return get_model_registry(settings).get_bundle()


def get_uow(request: Request) -> Generator[UnitOfWork, None, None]:
    settings: Settings = request.app.state.settings
    if not settings.database_url:
        raise AppError("INTERNAL_ERROR", "数据库未配置", 503)
    with UnitOfWork(get_session_factory(settings.database_url)) as uow:
        yield uow


def idempotency_key(
    key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if key is None or not key.strip():
        raise AppError("VALIDATION_ERROR", "写请求必须提供 Idempotency-Key", 422)
    if len(key) > 128:
        raise AppError("VALIDATION_ERROR", "Idempotency-Key 最长为 128 字符", 422)
    return key
