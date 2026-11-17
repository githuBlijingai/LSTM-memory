from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.logging import redact_text
from app.models import OutboxEvent, OutboxStatus
from app.repositories.base import CursorPage, paginate
from app.schemas.json_fields import OutboxPayload, to_json_dict
from app.services.audit import AuditLogService


class OutboxService:
    def __init__(
        self, session: Session, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.session = session
        self.clock = clock

    def enqueue(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        payload: OutboxPayload,
        available_at: datetime | None = None,
    ) -> OutboxEvent:
        event = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=to_json_dict(payload),
            available_at=available_at or self.clock(),
            status=OutboxStatus.PENDING,
            attempt_count=0,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def claim_batch(
        self, *, limit: int = 100, visibility_timeout: timedelta = timedelta(seconds=120)
    ) -> list[OutboxEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = self.clock()
        events = list(
            self.session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.status.in_(
                        [OutboxStatus.PENDING, OutboxStatus.FAILED, OutboxStatus.PROCESSING]
                    ),
                    OutboxEvent.available_at <= now,
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.outbox_id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for event in events:
            event.status = OutboxStatus.PROCESSING
            event.attempt_count += 1
            event.available_at = now + visibility_timeout
        self.session.flush()
        return events

    def mark_processed(self, event: OutboxEvent) -> None:
        event.status = OutboxStatus.PROCESSED
        event.processed_at = self.clock()
        event.last_error = None
        self.session.flush()

    def mark_failed(
        self,
        event: OutboxEvent,
        error: str,
        *,
        max_attempts: int = 5,
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        event.last_error = redact_text(error)[:1000]
        event.processed_at = None
        if event.attempt_count >= max_attempts:
            event.status = OutboxStatus.DEAD_LETTER
        else:
            event.status = OutboxStatus.FAILED
            event.available_at = self.clock() + retry_delay
        self.session.flush()

    def list_dead_letters(
        self,
        *,
        event_type: str | None = None,
        aggregate_type: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CursorPage[OutboxEvent]:
        statement = select(OutboxEvent).where(OutboxEvent.status == OutboxStatus.DEAD_LETTER)
        if event_type:
            statement = statement.where(OutboxEvent.event_type == event_type)
        if aggregate_type:
            statement = statement.where(OutboxEvent.aggregate_type == aggregate_type)
        return paginate(
            self.session,
            OutboxEvent,
            statement=statement,
            cursor=cursor,
            limit=limit,
        )

    def replay_dead_letter(
        self, outbox_id: UUID, *, actor_id: str, reason: str
    ) -> OutboxEvent:
        event = self.session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.outbox_id == outbox_id)
            .with_for_update()
        )
        if event is None:
            raise NotFoundError("Outbox 记录不存在")
        if event.status != OutboxStatus.DEAD_LETTER:
            raise AppError("CONFLICT", "仅死信状态的 Outbox 记录可以重放", 409)
        before = {"status": event.status.value, "attempt_count": event.attempt_count}
        event.status = OutboxStatus.PENDING
        event.attempt_count = 0
        event.last_error = None
        event.processed_at = None
        event.available_at = self.clock()
        self.session.flush()
        AuditLogService(self.session).record(
            actor_id=actor_id,
            action="outbox.replay",
            resource_type="outbox_event",
            resource_id=event.outbox_id,
            reason=reason,
            before=before,
            after={"status": event.status.value, "attempt_count": event.attempt_count},
        )
        return event
