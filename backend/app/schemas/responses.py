from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.request_context import get_request_id


class ResponseMeta(BaseModel):
    request_id: UUID
    next_cursor: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
    meta: ResponseMeta


class SuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: Any
    meta: ResponseMeta


def success_payload(data: Any, next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {"request_id": str(get_request_id()), "next_cursor": next_cursor},
    }


def error_payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "meta": {"request_id": str(get_request_id()), "next_cursor": None},
    }
