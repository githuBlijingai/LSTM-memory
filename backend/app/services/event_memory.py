from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.domain.sequence import is_late_event, left_padded_mask, pad_window_vectors
from app.domain.state import next_sequence_no
from app.ml.event_encoder import EventEncoder
from app.ml.registry import ModelBundle
from app.models import (
    BackgroundJobType,
    Entity,
    EntityStatus,
    EventMemory,
    EventStatus,
)
from app.schemas.json_fields import BackgroundJobPayload
from app.services.audit import AuditLogService
from app.services.entity_state import (
    EntityStateService,
    TensorState,
    acquire_entity_lock,
    acquire_name_lock,
)
from app.services.jobs import BackgroundJobService
from app.services.summary import SummaryEvent, SummaryProvider, TemplateSummaryProvider
from app.services.uow import UnitOfWork

logger = structlog.get_logger(__name__)

RETRYABLE_MODEL_CODES = {"MODEL_NETWORK_ERROR", "MODEL_RATE_LIMITED", "STATE_VERSION_CONFLICT"}
SUMMARY_IMPORTANT_LIMIT = 5
SUMMARY_FAMILIAR_LIMIT = 5


class RetryablePipelineError(AppError):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__("PIPELINE_RETRYABLE", message, 503, details)


@dataclass(frozen=True)
class PipelineResult:
    event_id: UUID
    status: Literal["processed", "already_processed", "deferred"]
    state_version: int
    sequence_no: int | None
    rebuild_job_id: UUID | None = None


class EventPipelineService:
    """事件写入管道核心；供 Worker 调用，事务由 UnitOfWork 管理。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        models: ModelBundle,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        summary_provider: SummaryProvider | None = None,
        encoder: EventEncoder | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.models = models
        self.clock = clock
        self.summary_provider = summary_provider or TemplateSummaryProvider()
        self.encoder = encoder or models.event_encoder

    def process_event(self, event_id: UUID, *, entity_type: str = "user") -> PipelineResult:
        snapshot = None
        with UnitOfWork(self.session_factory) as uow:
            session = uow.session
            event = session.scalar(
                select(EventMemory).where(EventMemory.event_id == event_id).with_for_update()
            )
            if event is None:
                raise NotFoundError("事件不存在")
            if event.status in {EventStatus.ACTIVE, EventStatus.ARCHIVED, EventStatus.PURGED}:
                return PipelineResult(
                    event_id=event.event_id,
                    status="already_processed",
                    state_version=self._entity_version(session, event.entity_id),
                    sequence_no=event.sequence_no,
                )

            entity = self._resolve_entity(session, event, entity_type=entity_type)
            entity_id_value = entity.entity_id
            acquire_entity_lock(session, entity.entity_id)
            session.refresh(entity)
            session.refresh(event)
            if event.status in {EventStatus.ACTIVE, EventStatus.ARCHIVED, EventStatus.PURGED}:
                return PipelineResult(
                    event_id=event.event_id,
                    status="already_processed",
                    state_version=entity.current_state_version,
                    sequence_no=event.sequence_no,
                )

            previous_last_event_time = entity.last_event_time
            deferred_job_id, previous_state, init_state = self._load_previous_state(
                session, entity, event
            )
            if deferred_job_id is not None:
                uow.commit()
                return PipelineResult(
                    event_id=event.event_id,
                    status="deferred",
                    state_version=entity.current_state_version,
                    sequence_no=event.sequence_no,
                    rebuild_job_id=deferred_job_id,
                )
            late = is_late_event(event.event_time, previous_last_event_time)
            sequence_no = event.sequence_no or next_sequence_no(entity.last_sequence_no)
            base_vectors, mask = self._encode_window(session, entity, event)
            state_service = self._state_service(session)

            try:
                snapshot, transition = state_service.advance(
                    entity,
                    base_vectors=base_vectors,
                    mask=mask,
                    init_state=init_state,
                    previous=previous_state,
                    last_event_id=event.event_id,
                    last_sequence_no=sequence_no,
                    model_version=self.models.event_model_version,
                    expected_state_version=entity.current_state_version,
                )
            except AppError as exc:
                wrapped = self._wrap_model_error(exc)
                if wrapped is None:
                    raise
                raise wrapped from exc

            snapshot_version = snapshot.state_version
            previous_status = event.status
            event.sequence_no = sequence_no
            event.embedding = transition.event_vector
            event.event_model_version = self.models.event_model_version
            event.status = EventStatus.ACTIVE
            event.failure_reason = None
            entity.last_sequence_no = sequence_no
            entity.last_event_id = event.event_id
            if previous_last_event_time is None or event.event_time > previous_last_event_time:
                entity.last_event_time = event.event_time
            self._update_summary(session, entity, event)

            rebuild_job_id = None
            if late:
                job = self._create_rebuild_job(
                    session,
                    entity,
                    event,
                    operation="late_event",
                    target_state_version=snapshot.state_version,
                )
                entity.status = EntityStatus.REBUILDING
                rebuild_job_id = job.job_id
            AuditLogService(session).record(
                actor_id=event.owner_id,
                action="event.pipeline.completed",
                resource_type="event",
                resource_id=event.event_id,
                reason="事件编码与实体状态推进",
                before={"status": str(previous_status)},
                after={
                    "status": EventStatus.ACTIVE.value,
                    "state_version": snapshot.state_version,
                    "sequence_no": sequence_no,
                },
                request_id=uuid4(),
            )
            session.flush()
            uow.commit()
            # commit 后属性会过期，刷新后再写 Redis 缓存，避免同步缓存读取失效实例。
            session.refresh(snapshot)
            state_service.sync_cache(snapshot)

        logger.info(
            "event_pipeline_completed",
            event_id=str(event_id),
            entity_id=str(entity_id_value),
            state_version=snapshot_version,
            sequence_no=sequence_no,
            late=late,
        )
        return PipelineResult(
            event_id=event_id,
            status="processed",
            state_version=snapshot_version,
            sequence_no=sequence_no,
            rebuild_job_id=rebuild_job_id,
        )

    # ---------- 内部步骤 ----------

    def _state_service(self, session: Session) -> EntityStateService:
        return EntityStateService(
            session,
            self.settings,
            encoder=self.encoder,
            clock=self.clock,
        )

    def _entity_version(self, session: Session, entity_id: UUID) -> int:
        entity = session.get(Entity, entity_id)
        return entity.current_state_version if entity is not None else 0

    def _resolve_entity(self, session: Session, event: EventMemory, *, entity_type: str) -> Entity:
        normalized_type = (entity_type or "user")[:32]
        acquire_name_lock(
            session,
            owner_id=event.owner_id,
            entity_name=event.subject,
            entity_type=normalized_type,
        )
        entity = session.scalar(
            select(Entity)
            .where(
                Entity.owner_id == event.owner_id,
                Entity.entity_name == event.subject,
                Entity.entity_type == normalized_type,
            )
            .with_for_update()
            .limit(1)
        )
        if entity is None:
            current = session.get(Entity, event.entity_id)
            if current is not None and self._entity_matches(current, event, normalized_type):
                entity = current
            else:
                entity = Entity(
                    entity_name=event.subject[:128],
                    entity_type=normalized_type,
                    owner_id=event.owner_id,
                    visibility=event.visibility,
                    status=EntityStatus.ACTIVE,
                )
                session.add(entity)
                session.flush()
        if event.entity_id != entity.entity_id:
            event.entity_id = entity.entity_id
            session.flush()
        return entity

    @staticmethod
    def _entity_matches(entity: Entity, event: EventMemory, entity_type: str) -> bool:
        return (
            entity.owner_id == event.owner_id
            and entity.entity_name == event.subject
            and entity.entity_type == entity_type
        )

    def _load_previous_state(
        self, session: Session, entity: Entity, event: EventMemory
    ) -> tuple[UUID | None, TensorState | None, tuple | None]:
        if entity.current_state_version == 0:
            return None, None, None
        state_service = self._state_service(session)
        snapshot = state_service.latest_snapshot(
            entity.entity_id, state_version=entity.current_state_version
        )
        if snapshot is None:
            job = self._create_rebuild_job(
                session,
                entity,
                event,
                operation="missing_snapshot",
                target_state_version=entity.current_state_version,
            )
            entity.status = EntityStatus.REBUILDING
            session.flush()
            return job.job_id, None, None
        if snapshot.model_version != self.models.event_model_version:
            job = self._create_rebuild_job(
                session,
                entity,
                event,
                operation="model_upgrade",
                target_state_version=entity.current_state_version,
            )
            entity.status = EntityStatus.REBUILDING
            session.flush()
            return job.job_id, None, None
        previous = state_service.snapshot_to_state(snapshot)
        init_state = state_service.decode(previous.serialized)
        return None, previous, init_state

    def _encode_window(
        self, session: Session, entity: Entity, event: EventMemory
    ) -> tuple[list[list[float]], list[float]]:
        window_size = self.settings.event_window_size
        recent = list(
            session.scalars(
                select(EventMemory)
                .where(
                    EventMemory.entity_id == entity.entity_id,
                    EventMemory.status == EventStatus.ACTIVE,
                )
                .order_by(EventMemory.event_time.desc(), EventMemory.event_id.desc())
                .limit(window_size)
            ).all()
        )
        window = [*recent, event]
        window.sort(key=lambda item: (item.event_time, str(item.event_id)))
        window = window[-window_size:]
        texts = [(item.raw_content or item.title or "") for item in window]
        try:
            base_vectors = self.models.embedding.embed(texts)
        except AppError as exc:
            wrapped = self._wrap_model_error(exc)
            if wrapped is None:
                raise
            raise wrapped from exc
        if len(base_vectors) != len(window):
            raise RetryablePipelineError("Embedding 返回数量与事件窗口不一致")
        mask = list(left_padded_mask(len(window), window_size))
        return pad_window_vectors(base_vectors, window_size=window_size), mask

    def _update_summary(self, session: Session, entity: Entity, current_event: EventMemory) -> None:
        candidates: list[EventMemory] = [current_event]
        candidates.extend(
            session.scalars(
                select(EventMemory)
                .where(
                    EventMemory.entity_id == entity.entity_id,
                    EventMemory.status == EventStatus.ACTIVE,
                    EventMemory.importance >= self.settings.working_memory_important_threshold,
                )
                .order_by(EventMemory.importance.desc(), EventMemory.event_time.desc())
                .limit(SUMMARY_IMPORTANT_LIMIT)
            ).all()
        )
        candidates.extend(
            session.scalars(
                select(EventMemory)
                .where(
                    EventMemory.entity_id == entity.entity_id,
                    EventMemory.status == EventStatus.ACTIVE,
                    EventMemory.familiarity_score >= self.settings.replay_threshold,
                )
                .order_by(EventMemory.familiarity_score.desc(), EventMemory.event_time.desc())
                .limit(SUMMARY_FAMILIAR_LIMIT)
            ).all()
        )
        unique = self._dedupe_events(candidates)
        summary = self.summary_provider.summarize(
            entity.entity_name,
            [
                SummaryEvent(
                    event_id=item.event_id,
                    title=item.title,
                    event_type=item.event_type,
                    event_time=item.event_time,
                    importance=item.importance,
                    familiarity_score=item.familiarity_score,
                )
                for item in unique
            ],
        )
        entity.readable_summary = summary.text
        entity.summary_source_event_ids = summary.source_event_ids
        entity.summary_version = summary.version
        entity.summary_updated_at = self.clock()

    @staticmethod
    def _dedupe_events(events: Iterable[EventMemory]) -> list[EventMemory]:
        seen: set[UUID] = set()
        unique = []
        for event in events:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            unique.append(event)
        return unique

    def _create_rebuild_job(
        self,
        session: Session,
        entity: Entity,
        event: EventMemory,
        *,
        operation: str,
        target_state_version: int,
    ):
        return BackgroundJobService(session, clock=self.clock).create(
            job_type=BackgroundJobType.STATE_REBUILD,
            resource_type="entity",
            resource_id=entity.entity_id,
            payload=BackgroundJobPayload(
                operation=operation,
                entity_id=entity.entity_id,
                event_id=event.event_id,
                target_state_version=target_state_version,
            ),
            requested_by=event.owner_id,
        )

    @staticmethod
    def _wrap_model_error(error: AppError) -> RetryablePipelineError | None:
        if error.code in RETRYABLE_MODEL_CODES:
            return RetryablePipelineError(error.message, error.details)
        return None
