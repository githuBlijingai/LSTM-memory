from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.core.request_context import get_request_id
from app.core.security import CurrentUser
from app.schemas.json_fields import IdempotencyResponseBody, to_json_dict
from app.schemas.responses import success_payload
from app.services.idempotency import IdempotencyService, hash_request
from app.services.uow import UnitOfWork


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return to_json_dict(value)
    return value


def execute_idempotent(
    *,
    uow: UnitOfWork,
    user: CurrentUser,
    key: str,
    method: str,
    path: str,
    body: Any,
    action: Callable[[], tuple[Any, UUID | None]],
    status_code: int = 202,
) -> tuple[dict[str, Any], int]:
    """在单个事务内执行幂等写入；重放时返回首次响应数据。"""
    service = IdempotencyService(uow.session)
    result = service.begin(
        actor_id=user.subject,
        key=key,
        request_path=path,
        request_hash=hash_request(method, path, body),
    )
    if result.replay and result.record.response_body is not None:
        stored = result.record.response_body
        return success_payload(stored.get("data")), result.record.response_status or status_code
    data, resource_id = action()
    serialized = _jsonable(data)
    service.complete(
        result.record,
        response_status=status_code,
        response_body=IdempotencyResponseBody(
            data=serialized,
            request_id=get_request_id(),
        ),
        resource_id=resource_id,
    )
    uow.commit()
    return success_payload(serialized), status_code
