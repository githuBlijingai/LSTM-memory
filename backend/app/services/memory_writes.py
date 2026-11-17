from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.security import CurrentUser
from app.models import (
    BackgroundJob,
    BackgroundJobType,
    Entity,
    EventMemory,
    EventStatus,
    Visibility,
)
from app.repositories.permissions import can_view_memory
from app.schemas.json_fields import BackgroundJobPayload, OutboxPayload
from app.schemas.memory import EventCorrectionRequest, EventCreateRequest, EventDeleteRequest
from app.services.audit import AuditLogService
from app.services.jobs import BackgroundJobService
from app.services.outbox import OutboxService
from app.services.working_memory import ConsolidationDraft


def content_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventWriteOutcome:
    event: EventMemory
    job: BackgroundJob


@dataclass(frozen=True)
class EventModificationOutcome:
    event: EventMemory
    rebuild_job: BackgroundJob | None


class EventCommandService:
    """事件写入、纠偏、删除、恢复与工作记忆巩固写事件。"""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.settings = settings
        self.clock = clock
        self.jobs = BackgroundJobService(session, clock)
        self.outbox = OutboxService(session, clock)
        self.audit = AuditLogService(session)

    # ---- 写入 -------------------------------------------------------------

    def create_event(self, user: CurrentUser, request: EventCreateRequest) -> EventWriteOutcome:
        entity = self._resolve_entity(
            owner_id=user.subject,
            name=request.subject,
            entity_type=request.entity_type,
            visibility=request.visibility,
        )
        event = self._new_event(
            user=user,
            entity=entity,
            subject=request.subject,
            event_type=request.event_type,
            event_time=request.event_time,
            title=request.title,
            content=request.content,
            importance=request.importance,
            visibility=request.visibility,
            source=request.source,
            source_event_key=request.source_event_key,
        )
        job = self._enqueue_pipeline(user=user, event=event)
        return EventWriteOutcome(event=event, job=job)

    def create_consolidated_event(self, user: CurrentUser, draft: ConsolidationDraft) -> UUID:
        source_event_key = f"{draft.session_id}:{draft.content_hash}"
        existing = self.session.scalar(
            select(EventMemory).where(
                EventMemory.owner_id == user.subject,
                EventMemory.source == "working_memory",
                EventMemory.source_event_key == source_event_key,
            )
        )
        if existing is not None:
            return existing.event_id
        entity = self._resolve_entity(
            owner_id=user.subject,
            name=user.subject,
            entity_type="user",
            visibility=Visibility.PRIVATE,
        )
        title = draft.content[:256]
        event = self._new_event(
            user=user,
            entity=entity,
            subject=user.subject,
            event_type=draft.event_type,
            event_time=self.clock(),
            title=title,
            content=draft.content,
            importance=draft.importance,
            visibility=Visibility.PRIVATE,
            source="working_memory",
            source_event_key=source_event_key,
        )
        self._enqueue_pipeline(user=user, event=event)
        return event.event_id

    def correct_event(
        self,
        user: CurrentUser,
        event_id: UUID,
        request: EventCorrectionRequest,
    ) -> EventModificationOutcome:
        if not user.is_admin:
            raise AppError("FORBIDDEN", "仅管理员可以纠偏事件", 403)
        old = self._load_event(user, event_id, for_update=True)
        if old.status == EventStatus.PURGED:
            raise AppError("CONFLICT", "已清除事件不能纠偏", 409)
        new_event = self._new_event(
            user=user,
            entity=self.session.get(Entity, old.entity_id),
            subject=old.subject,
            event_type=old.event_type,
            event_time=request.event_time or old.event_time,
            title=request.title or f"{old.title}（修正）",
            content=request.content,
            importance=old.importance,
            visibility=old.visibility,
            source="correction",
            source_event_key=None,
            supersedes_event_id=old.event_id,
        )
        self._enqueue_pipeline(user=user, event=new_event)
        now = self.clock()
        old.status = EventStatus.ARCHIVED
        old.archived_at = now
        old.purge_after = None
        self.audit.record(
            actor_id=user.subject,
            action="event.correct",
            resource_type="event",
            resource_id=old.event_id,
            reason=request.reason,
            before={"status": "active", "content_hash": old.content_hash},
            after={"new_event_id": str(new_event.event_id), "status": "archived"},
        )
        job = self._create_rebuild_job(
            user=user,
            entity_id=old.entity_id,
            operation="event_correction",
            event_id=old.event_id,
        )
        self.session.flush()
        return EventModificationOutcome(event=new_event, rebuild_job=job)

    def delete_event(
        self, user: CurrentUser, event_id: UUID, request: EventDeleteRequest
    ) -> EventModificationOutcome:
        if not user.is_admin:
            raise AppError("FORBIDDEN", "仅管理员可以归档或清除事件", 403)
        event = self._load_event(user, event_id, for_update=True)
        if event.status == EventStatus.PURGED:
            return EventModificationOutcome(event=event, rebuild_job=None)
        if event.status == EventStatus.ARCHIVED and request.mode == "archive":
            return EventModificationOutcome(event=event, rebuild_job=None)

        now = self.clock()
        before = {"status": event.status.value, "content_hash": event.content_hash}
        rebuild_needed = event.status == EventStatus.ACTIVE
        if request.mode == "archive":
            event.status = EventStatus.ARCHIVED
            event.archived_at = now
            event.purge_after = now + timedelta(days=self.settings.archive_retention_days)
        else:
            event.status = EventStatus.PURGED
            event.archived_at = now
            event.deleted_at = now
            event.purge_after = None
            event.raw_content = None
            event.embedding = None
            event.title = "［已清除］"
        self.audit.record(
            actor_id=user.subject,
            action=f"event.{request.mode}",
            resource_type="event",
            resource_id=event.event_id,
            reason=request.reason,
            before=before,
            after={"status": event.status.value},
        )
        job = (
            self._create_rebuild_job(
                user=user,
                entity_id=event.entity_id,
                operation=f"event_{request.mode}d",
                event_id=event.event_id,
            )
            if rebuild_needed
            else None
        )
        self.session.flush()
        return EventModificationOutcome(event=event, rebuild_job=job)

    def restore_event(
        self, user: CurrentUser, event_id: UUID, reason: str
    ) -> EventModificationOutcome:
        if not user.is_admin:
            raise AppError("FORBIDDEN", "仅管理员可以恢复事件", 403)
        event = self._load_event(user, event_id, for_update=True)
        if event.status != EventStatus.ARCHIVED:
            raise AppError("INVALID_STATE", "只有归档事件可以恢复", 409)
        event.status = EventStatus.ACTIVE if event.embedding is not None else EventStatus.PENDING
        event.archived_at = None
        event.purge_after = None
        event.deleted_at = None
        self.audit.record(
            actor_id=user.subject,
            action="event.restore",
            resource_type="event",
            resource_id=event.event_id,
            reason=reason,
            before={"status": "archived"},
            after={"status": event.status.value},
        )
        job = self._create_rebuild_job(
            user=user,
            entity_id=event.entity_id,
            operation="event_restored",
            event_id=event.event_id,
        )
        self.session.flush()
        return EventModificationOutcome(event=event, rebuild_job=job)

    # ---- 内部 -------------------------------------------------------------

    def _new_event(
        self,
        *,
        user: CurrentUser,
        entity: Entity | None,
        subject: str,
        event_type: str,
        event_time: datetime,
        title: str,
        content: str,
        importance: float,
        visibility: Visibility,
        source: str,
        source_event_key: str | None,
        supersedes_event_id: UUID | None = None,
    ) -> EventMemory:
        if entity is None:
            raise NotFoundError("实体不存在")
        event = EventMemory(
            entity_id=entity.entity_id,
            subject=subject,
            event_type=event_type,
            event_time=event_time,
            title=title.strip()[:256],
            raw_content=content,
            content_hash=content_hash(content),
            importance=importance,
            source=source,
            source_event_key=source_event_key,
            visibility=visibility,
            owner_id=user.subject,
            status=EventStatus.PENDING,
            supersedes_event_id=supersedes_event_id,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _resolve_entity(
        self,
        *,
        owner_id: str,
        name: str,
        entity_type: str,
        visibility: Visibility,
    ) -> Entity:
        entity = self.session.scalar(
            select(Entity)
            .where(
                Entity.owner_id == owner_id,
                Entity.entity_name == name,
                Entity.entity_type == entity_type,
            )
            .with_for_update()
        )
        if entity is not None:
            return entity
        entity = Entity(
            entity_name=name,
            entity_type=entity_type,
            owner_id=owner_id,
            visibility=visibility,
        )
        self.session.add(entity)
        self.session.flush()
        return entity

    def _enqueue_pipeline(self, *, user: CurrentUser, event: EventMemory) -> BackgroundJob:
        job = self.jobs.create(
            job_type=BackgroundJobType.EVENT_PIPELINE,
            resource_type="event",
            resource_id=event.event_id,
            payload=BackgroundJobPayload(
                operation="event_created",
                event_id=event.event_id,
                entity_id=event.entity_id,
            ),
            requested_by=user.subject,
        )
        self.outbox.enqueue(
            aggregate_type="event",
            aggregate_id=event.event_id,
            event_type="memory.event.created",
            payload=OutboxPayload(
                event_id=event.event_id,
                entity_id=event.entity_id,
                job_id=job.job_id,
                operation="event_created",
            ),
        )
        return job

    def _create_rebuild_job(
        self, *, user: CurrentUser, entity_id: UUID, operation: str, event_id: UUID
    ) -> BackgroundJob:
        job = self.jobs.create(
            job_type=BackgroundJobType.STATE_REBUILD,
            resource_type="entity",
            resource_id=entity_id,
            payload=BackgroundJobPayload(
                operation=operation,
                entity_id=entity_id,
                event_id=event_id,
            ),
            requested_by=user.subject,
        )
        # 复用 Outbox 派发链路，避免请求线程直接依赖 Celery Broker。
        self.outbox.enqueue(
            aggregate_type="entity",
            aggregate_id=entity_id,
            event_type="memory.state.rebuild.requested",
            payload=OutboxPayload(
                entity_id=entity_id,
                event_id=event_id,
                job_id=job.job_id,
                operation=operation,
            ),
        )
        return job

    def _load_event(self, user: CurrentUser, event_id: UUID, *, for_update: bool) -> EventMemory:
        event = self.session.get(EventMemory, event_id, with_for_update=for_update)
        if event is None or not can_view_memory(user, event):
            raise NotFoundError("事件不存在")
        return event
