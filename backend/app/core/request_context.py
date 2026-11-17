from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID, uuid4

_request_id: ContextVar[UUID | None] = ContextVar("request_id", default=None)


def get_request_id() -> UUID:
    value = _request_id.get()
    if value is None:
        value = uuid4()
        _request_id.set(value)
    return value


def set_request_id(value: UUID) -> None:
    _request_id.set(value)


def new_request_id(candidate: str | None = None) -> UUID:
    if candidate:
        try:
            value = UUID(candidate)
        except (ValueError, AttributeError):
            value = uuid4()
    else:
        value = uuid4()
    set_request_id(value)
    return value
