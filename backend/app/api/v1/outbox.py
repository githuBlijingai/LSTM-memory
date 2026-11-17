from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.security import CurrentUser, require_admin
from app.models import OutboxEvent
from app.schemas.outbox import OutboxDeadLetterItem, OutboxReplayResponse
from app.schemas.responses import success_payload
from app.services.outbox import OutboxService
from app.services.uow import UnitOfWork

router = APIRouter(prefix="/outbox", tags=["outbox"])


def _item(event: OutboxEvent) -> OutboxDeadLetterItem:
    return OutboxDeadLetterItem(
        outbox_id=event.outbox_id,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        payload=event.payload if isinstance(event.payload, dict) else {},
        status=event.status.value,
        attempt_count=event.attempt_count,
        last_error=event.last_error,
        available_at=event.available_at,
        created_at=event.created_at,
        processed_at=event.processed_at,
    )


@router.get("/dead-letters")
def list_dead_letters(
    event_type: str | None = Query(default=None, max_length=64),
    aggregate_type: str | None = Query(default=None, max_length=32),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    uow: UnitOfWork = Depends(get_uow),
    user: CurrentUser = Depends(require_admin),
) -> dict:
    page = OutboxService(uow.session).list_dead_letters(
        event_type=event_type,
        aggregate_type=aggregate_type,
        cursor=cursor,
        limit=limit,
    )
    return success_payload(
        [_item(event).model_dump(mode="json") for event in page.items],
        page.next_cursor,
    )


@router.post("/dead-letters/{outbox_id}/replay", status_code=202)
def replay_dead_letter(
    outbox_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = OutboxService(uow.session)

    def action():
        event = service.replay_dead_letter(
            outbox_id,
            actor_id=user.subject,
            reason="管理员手动重放 Outbox 死信",
        )
        return (
            OutboxReplayResponse(
                outbox_id=event.outbox_id,
                status=event.status.value,
                attempt_count=event.attempt_count,
                available_at=event.available_at,
            ),
            event.outbox_id,
        )

    body, _ = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
    )
    return body
