from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.security import CurrentUser, Role
from app.domain.scoring import materialize_familiarity
from app.models import (
    BackgroundJob,
    BackgroundJobType,
    Entity,
    EntityStateSnapshot,
    EventMemory,
    EventStatus,
    FeedbackType,
    JobStatus,
    MemoryAccessLog,
    OutboxEvent,
    OutboxStatus,
    ReplayStatus,
    StateReplayLog,
    Visibility,
    WikiArticle,
    WikiArticleStatus,
    WikiChunk,
    WikiChunkStatus,
)
from app.repositories.base import CursorPage, decode_cursor, encode_cursor
from app.repositories.permissions import can_view_memory, visibility_predicate
from app.schemas.json_fields import BackgroundJobPayload, OutboxPayload
from app.schemas.memory import (
    DashboardSummaryResponse,
    EntityDetailResponse,
    EntityRebuildResponse,
    EntityStateResponse,
    EntitySummaryResponse,
    EventDetailResponse,
    EventSummaryResponse,
    ReplayLogResponse,
)
from app.services.audit import AuditLogService
from app.services.jobs import BackgroundJobService
from app.services.outbox import OutboxService


@dataclass(frozen=True)
class EventListPage:
    items: list[EventSummaryResponse]
    next_cursor: str | None


def _event_summary(event: EventMemory) -> EventSummaryResponse:
    return EventSummaryResponse(
        event_id=event.event_id,
        subject=event.subject,
        event_type=event.event_type,
        title=event.title,
        event_time=event.event_time,
        importance=event.importance,
        status=event.status.value,
        visibility=event.visibility,
        created_at=event.created_at,
    )


class MemoryReadService:
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

    # ---- 事件读取 ---------------------------------------------------------

    def list_events(
        self,
        user: CurrentUser,
        *,
        subject: str | None,
        entity_id: UUID | None,
        event_type: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        status: str | None,
        cursor: str | None,
        limit: int,
        owner_id: str | None = None,
        descending: bool = True,
    ) -> EventListPage:
        statement = select(EventMemory).where(
            visibility_predicate(user, EventMemory.owner_id, EventMemory.visibility)
        )
        if subject:
            statement = statement.where(EventMemory.subject == subject)
        if entity_id:
            statement = statement.where(EventMemory.entity_id == entity_id)
        if event_type:
            statement = statement.where(EventMemory.event_type == event_type)
        if owner_id:
            statement = statement.where(EventMemory.owner_id == owner_id)
        if start_time:
            statement = statement.where(EventMemory.event_time >= start_time)
        if end_time:
            statement = statement.where(EventMemory.event_time <= end_time)
        if status:
            try:
                statement = statement.where(EventMemory.status == EventStatus(status))
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的事件状态", 422) from exc
        if cursor:
            try:
                event_time, last_id = decode_cursor(cursor)
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的分页游标", 422) from exc
            if descending:
                statement = statement.where(
                    (EventMemory.event_time < event_time)
                    | (
                        (EventMemory.event_time == event_time)
                        & (EventMemory.event_id < last_id)
                    )
                )
            else:
                statement = statement.where(
                    (EventMemory.event_time > event_time)
                    | (
                        (EventMemory.event_time == event_time)
                        & (EventMemory.event_id > last_id)
                    )
                )
        if descending:
            order_by = (EventMemory.event_time.desc(), EventMemory.event_id.desc())
        else:
            order_by = (EventMemory.event_time.asc(), EventMemory.event_id.asc())
        rows = list(
            self.session.scalars(statement.order_by(*order_by).limit(limit + 1)).all()
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].event_time, items[-1].event_id) if has_more and items else None
        )
        return EventListPage(
            items=[_event_summary(event) for event in items], next_cursor=next_cursor
        )

    def get_event(self, user: CurrentUser, event_id: UUID) -> EventDetailResponse:
        event = self._load_event(user, event_id)
        familiarity = self._event_familiarity(event)
        return EventDetailResponse(
            **_event_summary(event).model_dump(),
            content=event.raw_content,
            entity_id=event.entity_id,
            decay_score=event.decay_score,
            familiarity_score=familiarity,
            source=event.source,
            supersedes_event_id=event.supersedes_event_id,
            sequence_no=event.sequence_no,
            event_model_version=event.event_model_version,
            updated_at=event.updated_at,
        )

    # ---- 实体与状态 -------------------------------------------------------

    def list_entities(
        self,
        user: CurrentUser,
        *,
        entity_type: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[EntitySummaryResponse]:
        statement = select(Entity).where(
            visibility_predicate(user, Entity.owner_id, Entity.visibility)
        )
        if entity_type:
            statement = statement.where(Entity.entity_type == entity_type)
        if status:
            statement = statement.where(Entity.status == status)
        if cursor:
            try:
                created_at, last_id = decode_cursor(cursor)
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的分页游标", 422) from exc
            statement = statement.where(
                (Entity.created_at < created_at)
                | ((Entity.created_at == created_at) & (Entity.entity_id < last_id))
            )
        rows = list(
            self.session.scalars(
                statement.order_by(Entity.created_at.desc(), Entity.entity_id.desc()).limit(
                    limit + 1
                )
            ).all()
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].entity_id) if has_more and items else None
        )
        return CursorPage(
            items=[self._entity_summary(entity) for entity in items], next_cursor=next_cursor
        )

    def get_entity(self, user: CurrentUser, entity_id: UUID) -> EntityDetailResponse:
        entity = self._load_entity(user, entity_id)
        summary = self._entity_summary(entity)
        return EntityDetailResponse(
            **summary.model_dump(),
            owner_id=entity.owner_id,
            source_event_ids=[UUID(item) for item in entity.summary_source_event_ids or []],
            summary_version=entity.summary_version,
            summary_updated_at=entity.summary_updated_at,
            last_event_id=entity.last_event_id,
            last_event_time=entity.last_event_time,
        )

    def list_entity_events(
        self,
        user: CurrentUser,
        entity_id: UUID,
        *,
        event_type: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> EventListPage:
        self._load_entity(user, entity_id)
        return self.list_events(
            user,
            subject=None,
            entity_id=entity_id,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
            status=None,
            cursor=cursor,
            limit=limit,
        )

    def get_entity_state(self, user: CurrentUser, entity_id: UUID) -> EntityStateResponse:
        entity = self._load_entity(user, entity_id)
        latest_snapshot_at = self.session.scalar(
            select(func.max(EntityStateSnapshot.created_at)).where(
                EntityStateSnapshot.entity_id == entity_id
            )
        )
        pending_replays = self.session.scalar(
            select(func.count(StateReplayLog.replay_id)).where(
                StateReplayLog.entity_id == entity_id,
                StateReplayLog.status.in_([ReplayStatus.QUEUED, ReplayStatus.RUNNING]),
            )
        )
        active_rebuild = self.session.scalar(
            select(BackgroundJob.job_id).where(
                BackgroundJob.job_type == BackgroundJobType.STATE_REBUILD,
                BackgroundJob.resource_type == "entity",
                BackgroundJob.resource_id == entity_id,
                BackgroundJob.status.in_(
                    [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRY_WAIT]
                ),
            )
        )
        return EntityStateResponse(
            entity_id=entity.entity_id,
            status=entity.status.value,
            state_version=entity.current_state_version,
            model_version=entity.current_model_version,
            last_event_id=entity.last_event_id,
            last_sequence_no=entity.last_sequence_no,
            last_update_time=entity.last_update_time,
            latest_snapshot_at=latest_snapshot_at,
            pending_replay_count=int(pending_replays or 0),
            active_rebuild_job_id=active_rebuild,
        )

    def list_replays(
        self, user: CurrentUser, entity_id: UUID, *, limit: int
    ) -> list[ReplayLogResponse]:
        self._load_entity(user, entity_id)
        rows = self.session.scalars(
            select(StateReplayLog)
            .where(StateReplayLog.entity_id == entity_id)
            .order_by(StateReplayLog.created_at.desc())
            .limit(limit)
        ).all()
        return [
            ReplayLogResponse(
                replay_id=row.replay_id,
                event_id=row.event_id,
                reason=row.reason,
                before_state_version=row.before_state_version,
                after_state_version=row.after_state_version,
                model_version=row.model_version,
                status=row.status.value,
                attempt_count=row.attempt_count,
                error_message=row.error_message,
                created_at=row.created_at,
                completed_at=row.completed_at,
            )
            for row in rows
        ]

    def request_rebuild(self, user: CurrentUser, entity_id: UUID) -> EntityRebuildResponse:
        if not user.has_role(Role.ADMIN):
            raise AppError("FORBIDDEN", "需要 admin 角色", 403)
        entity = self._load_entity(user, entity_id)
        job = BackgroundJobService(self.session, self.clock).create(
            job_type=BackgroundJobType.STATE_REBUILD,
            resource_type="entity",
            resource_id=entity.entity_id,
            payload=BackgroundJobPayload(operation="manual_rebuild", entity_id=entity.entity_id),
            requested_by=user.subject,
        )
        OutboxService(self.session, self.clock).enqueue(
            aggregate_type="entity",
            aggregate_id=entity.entity_id,
            event_type="memory.state.rebuild.requested",
            payload=OutboxPayload(
                entity_id=entity.entity_id,
                job_id=job.job_id,
                operation="manual_rebuild",
            ),
        )
        return EntityRebuildResponse(entity_id=entity.entity_id, job_id=job.job_id, status="queued")

    # ---- 任务与仪表盘 -----------------------------------------------------

    def get_job(self, user: CurrentUser, job_id: UUID) -> BackgroundJob:
        job = self.session.get(BackgroundJob, job_id)
        if job is None:
            raise NotFoundError("任务不存在")
        if job.requested_by != user.subject and not user.has_role(Role.OPERATOR):
            raise NotFoundError("任务不存在")
        return job

    def retry_job(self, user: CurrentUser, job_id: UUID) -> BackgroundJob:
        job = self.get_job(user, job_id)
        BackgroundJobService(self.session, self.clock).retry(job)
        return job

    def dashboard_summary(self, user: CurrentUser) -> DashboardSummaryResponse:
        events = {
            status.value: int(count)
            for status, count in self.session.execute(
                select(EventMemory.status, func.count(EventMemory.event_id))
                .where(visibility_predicate(user, EventMemory.owner_id, EventMemory.visibility))
                .group_by(EventMemory.status)
            ).all()
        }
        wiki_active_articles = self.session.scalar(
            select(func.count(WikiArticle.article_id)).where(
                WikiArticle.status == WikiArticleStatus.ACTIVE
            )
        )
        wiki_active_chunks = self.session.scalar(
            select(func.count(WikiChunk.chunk_id)).where(WikiChunk.status == WikiChunkStatus.ACTIVE)
        )
        wiki_last_sync_at = self.session.scalar(select(func.max(WikiArticle.sync_time)))
        jobs = {
            status.value: int(count)
            for status, count in self.session.execute(
                select(BackgroundJob.status, func.count(BackgroundJob.job_id)).group_by(
                    BackgroundJob.status
                )
            ).all()
        }
        outbox = {
            status.value: int(count)
            for status, count in self.session.execute(
                select(OutboxEvent.status, func.count(OutboxEvent.outbox_id)).group_by(
                    OutboxEvent.status
                )
            ).all()
        }
        failed_feedback = self.session.scalar(
            select(func.count(MemoryAccessLog.log_id)).where(
                MemoryAccessLog.feedback_type == FeedbackType.NOT_HELPFUL
            )
        )
        return DashboardSummaryResponse(
            events=events,
            wiki={
                "active_articles": int(wiki_active_articles or 0),
                "active_chunks": int(wiki_active_chunks or 0),
                "last_sync_at": wiki_last_sync_at,
            },
            jobs=jobs,
            outbox=outbox,
            alerts={
                "dead_letter_jobs": jobs.get(JobStatus.DEAD_LETTER.value, 0),
                "dead_letter_outbox": outbox.get(OutboxStatus.DEAD_LETTER.value, 0),
                "failed_events": events.get(EventStatus.FAILED.value, 0),
                "not_helpful_feedback": int(failed_feedback or 0),
            },
        )

    # ---- 管理员熟悉度 -----------------------------------------------------

    def set_familiarity(
        self, user: CurrentUser, memory_type: str, memory_id: UUID, score: float, reason: str
    ) -> Any:
        if not user.is_admin:
            raise AppError("FORBIDDEN", "仅管理员可以手动调整熟悉度", 403)
        now = self.clock()
        if memory_type == "event":
            memory = self.session.get(EventMemory, memory_id, with_for_update=True)
            if memory is None:
                raise NotFoundError("事件不存在")
            before = memory.familiarity_score
            memory.familiarity_score = score
            memory.familiarity_updated_at = now
        elif memory_type == "wiki":
            memory = self.session.get(WikiChunk, memory_id, with_for_update=True)
            if memory is None:
                raise NotFoundError("Wiki 分块不存在")
            before = memory.familiarity_score
            memory.familiarity_score = score
            memory.familiarity_updated_at = now
        else:
            raise AppError("VALIDATION_ERROR", "memory_type 必须为 event 或 wiki", 422)
        AuditLogService(self.session).record(
            actor_id=user.subject,
            action="familiarity.update",
            resource_type=memory_type,
            resource_id=memory_id,
            reason=reason,
            before={"familiarity_score": before},
            after={"familiarity_score": score},
        )
        self.session.flush()
        return memory

    # ---- 内部 -------------------------------------------------------------

    def _load_event(self, user: CurrentUser, event_id: UUID) -> EventMemory:
        event = self.session.get(EventMemory, event_id)
        if event is None or not can_view_memory(user, event):
            raise NotFoundError("事件不存在")
        return event

    def _load_entity(self, user: CurrentUser, entity_id: UUID) -> Entity:
        entity = self.session.get(Entity, entity_id)
        if entity is None:
            raise NotFoundError("实体不存在")
        if (
            not user.is_admin
            and entity.owner_id != user.subject
            and entity.visibility != Visibility.SHARED
        ):
            raise NotFoundError("实体不存在")
        return entity

    def _event_familiarity(self, event: EventMemory) -> float:
        return materialize_familiarity(
            event.familiarity_score,
            anchor=event.familiarity_updated_at,
            created_at=event.created_at,
            clock=self.clock,
            gamma=self.settings.familiarity_gamma,
            max_score=self.settings.familiarity_max,
            protect_hours=self.settings.new_memory_protect_hours,
        ).score

    def _entity_summary(self, entity: Entity) -> EntitySummaryResponse:
        return EntitySummaryResponse(
            entity_id=entity.entity_id,
            entity_name=entity.entity_name,
            entity_type=entity.entity_type,
            status=entity.status.value,
            visibility=entity.visibility,
            readable_summary=entity.readable_summary,
            state_version=entity.current_state_version,
            model_version=entity.current_model_version,
            last_sequence_no=entity.last_sequence_no,
            last_update_time=entity.last_update_time,
        )
