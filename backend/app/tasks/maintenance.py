from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.domain.lifecycle import evaluate_auto_archive, purge_after
from app.domain.scoring import event_base_decay, materialize_familiarity
from app.models import (
    Entity,
    EntityStatus,
    EventMemory,
    EventStatus,
    WikiChunk,
    WikiChunkStatus,
    WorkingItem,
    WorkingSession,
    WorkingSessionStatus,
)
from app.services.audit import AuditLogService
from app.tasks.support import require_session_factory, retry_countdown
from app.worker import celery_app

logger = structlog.get_logger(__name__)

MAINTENANCE_MAX_RETRIES = 3
PURGED_TITLE = "[purged]"


def run_maintenance(
    session_factory: sessionmaker[Session] | Callable[[], Session],
    settings: Settings,
    now: datetime,
) -> dict[str, int]:
    with session_factory() as session:
        expired_sessions, cleared_items = _expire_working_memory(session, settings, now)
        stats = {
            "events_decayed": _decay_events(session, settings, now),
            "wiki_decayed": _decay_wiki(session, settings, now),
            "events_archived": _archive_events(session, settings, now),
            "events_purged": _purge_events(session, settings, now),
            "working_sessions_expired": expired_sessions,
            "working_items_cleared": cleared_items,
        }
        session.commit()
    logger.info("daily_maintenance_completed", **stats)
    return stats


def _expire_working_memory(session: Session, settings: Settings, now: datetime) -> tuple[int, int]:
    """清理已过期工作会话的原始临时条目，保留会话元数据与事件关联（设计 3.2）。"""
    expired_sessions = session.scalars(
        select(WorkingSession)
        .where(
            WorkingSession.status == WorkingSessionStatus.CLOSED,
            WorkingSession.expires_at.is_not(None),
            WorkingSession.expires_at <= now,
        )
        .order_by(WorkingSession.expires_at)
        .limit(settings.maintenance_batch_size)
    ).all()
    if not expired_sessions:
        return 0, 0
    session_ids = [working_session.session_id for working_session in expired_sessions]
    removed = session.execute(
        delete(WorkingItem).where(WorkingItem.session_id.in_(session_ids))
    ).rowcount
    for working_session in expired_sessions:
        working_session.status = WorkingSessionStatus.EXPIRED
    session.flush()
    return len(expired_sessions), int(removed or 0)


def _decay_events(session: Session, settings: Settings, now: datetime) -> int:
    def clock() -> datetime:
        return now

    events = session.scalars(
        select(EventMemory)
        .where(EventMemory.status.in_([EventStatus.ACTIVE, EventStatus.ARCHIVED]))
        .order_by(EventMemory.created_at)
        .limit(settings.maintenance_batch_size)
    ).all()
    for event in events:
        event.decay_score = event_base_decay(
            event.initial_decay_score,
            event.created_at,
            clock=clock,
            decay_lambda=settings.decay_lambda,
        )
        materialized = materialize_familiarity(
            event.familiarity_score,
            anchor=event.familiarity_updated_at,
            created_at=event.created_at,
            clock=clock,
            gamma=settings.familiarity_gamma,
            max_score=settings.familiarity_max,
            protect_hours=settings.new_memory_protect_hours,
        )
        event.familiarity_score = materialized.score
        event.familiarity_updated_at = materialized.updated_at
    session.flush()
    return len(events)


def _decay_wiki(session: Session, settings: Settings, now: datetime) -> int:
    def clock() -> datetime:
        return now

    chunks = session.scalars(
        select(WikiChunk)
        .where(WikiChunk.status == WikiChunkStatus.ACTIVE)
        .order_by(WikiChunk.created_at)
        .limit(settings.maintenance_batch_size)
    ).all()
    for chunk in chunks:
        materialized = materialize_familiarity(
            chunk.familiarity_score,
            anchor=chunk.familiarity_updated_at,
            created_at=chunk.created_at,
            clock=clock,
            gamma=settings.familiarity_gamma,
            max_score=settings.familiarity_max,
            protect_hours=settings.new_memory_protect_hours,
        )
        chunk.familiarity_score = materialized.score
        chunk.familiarity_updated_at = materialized.updated_at
    session.flush()
    return len(chunks)


def _archive_events(session: Session, settings: Settings, now: datetime) -> int:
    events = session.scalars(
        select(EventMemory)
        .where(EventMemory.status == EventStatus.ACTIVE)
        .order_by(EventMemory.created_at)
        .limit(settings.maintenance_batch_size)
    ).all()
    if not events:
        return 0
    entity_ids = {event.entity_id for event in events}
    entities = {
        entity.entity_id: entity
        for entity in session.scalars(select(Entity).where(Entity.entity_id.in_(entity_ids))).all()
    }
    pending_correction_ids = set(
        session.scalars(
            select(EventMemory.supersedes_event_id).where(
                EventMemory.supersedes_event_id.is_not(None),
                EventMemory.status == EventStatus.PENDING,
            )
        ).all()
    )
    archived = 0
    for event in events:
        entity = entities.get(event.entity_id)
        decision = evaluate_auto_archive(
            decay_score_value=event.decay_score,
            familiarity_score=event.familiarity_score,
            created_at=event.created_at,
            clock=lambda: now,
            delete_threshold=settings.delete_threshold,
            protect_hours=settings.new_memory_protect_hours,
            protected=event.protected,
            pending_correction=event.event_id in pending_correction_ids,
            pending_rebuild=entity is not None and entity.status == EntityStatus.REBUILDING,
            compliance_hold=False,
        )
        if not decision.eligible:
            continue
        event.status = EventStatus.ARCHIVED
        event.archived_at = now
        event.purge_after = purge_after(now, retention_days=settings.archive_retention_days)
        archived += 1
    session.flush()
    return archived


def _purge_events(session: Session, settings: Settings, now: datetime) -> int:
    events = session.scalars(
        select(EventMemory)
        .where(
            EventMemory.status == EventStatus.ARCHIVED,
            EventMemory.purge_after.is_not(None),
            EventMemory.purge_after <= now,
        )
        .order_by(EventMemory.purge_after)
        .limit(settings.maintenance_batch_size)
    ).all()
    audit = AuditLogService(session)
    for event in events:
        event.status = EventStatus.PURGED
        event.title = PURGED_TITLE
        event.raw_content = None
        event.embedding = None
        event.failure_reason = None
        event.deleted_at = now
        audit.record(
            actor_id="system",
            action="event.purged",
            resource_type="event",
            resource_id=event.event_id,
            reason="归档到期合规清除，仅保留最小审计墓碑",
            before={"status": EventStatus.ARCHIVED.value},
            after={"status": EventStatus.PURGED.value},
            request_id=uuid4(),
        )
    session.flush()
    return len(events)


@celery_app.task(
    bind=True,
    name="app.tasks.maintenance.run_daily_maintenance",
    max_retries=MAINTENANCE_MAX_RETRIES,
)
def run_daily_maintenance(self) -> dict[str, int]:
    settings = get_settings()
    factory = require_session_factory(settings)
    try:
        return run_maintenance(factory, settings, datetime.now(UTC))
    except Exception as exc:
        retries = self.request.retries
        if retries >= self.max_retries:
            logger.error(
                "daily_maintenance_dead_letter",
                retries=retries,
                error_type=type(exc).__name__,
            )
            raise
        raise self.retry(exc=exc, countdown=retry_countdown(retries)) from exc
