from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.security import CurrentUser
from app.domain.retrieval import daily_gain as clamp_daily_gain
from app.domain.scoring import materialize_familiarity
from app.models import (
    BackgroundJobType,
    EventMemory,
    FamiliarityDailyGain,
    FeedbackType,
    MemoryAccessLog,
    MemoryQuery,
    MemoryType,
    WikiChunk,
)
from app.repositories.permissions import can_view_memory, can_view_wiki
from app.schemas.json_fields import BackgroundJobPayload
from app.schemas.memory import FeedbackRequest, FeedbackResponse
from app.services.jobs import BackgroundJobService

POSITIVE_FEEDBACK = {FeedbackType.ADOPTED, FeedbackType.HELPFUL}
NOT_CONSOLIDATING_FEEDBACK = {FeedbackType.CLICKED, FeedbackType.NOT_HELPFUL}


@dataclass(frozen=True)
class FeedbackOutcome:
    response: FeedbackResponse
    replay_event_id: UUID | None = None
    replay_before_state_version: int | None = None


class FeedbackService:
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

    def submit(self, *, user: CurrentUser, request: FeedbackRequest) -> FeedbackOutcome:
        if request.feedback_type == FeedbackType.EXPOSURE:
            raise AppError("VALIDATION_ERROR", "反馈类型不能为 exposure", 422)
        query = self._load_query(user, request.query_id)
        exposure = self.session.scalar(
            select(MemoryAccessLog).where(
                MemoryAccessLog.query_id == query.query_id,
                MemoryAccessLog.memory_type == MemoryType(request.memory_type),
                MemoryAccessLog.memory_id == request.memory_id,
                MemoryAccessLog.feedback_type == FeedbackType.EXPOSURE,
            )
        )
        if exposure is None:
            raise NotFoundError("该查询结果不存在，无法提交反馈")

        existing = self.session.scalar(
            select(MemoryAccessLog).where(
                MemoryAccessLog.query_id == query.query_id,
                MemoryAccessLog.memory_type == MemoryType(request.memory_type),
                MemoryAccessLog.memory_id == request.memory_id,
                MemoryAccessLog.feedback_type == request.feedback_type,
            )
        )
        if existing is not None:
            return FeedbackOutcome(
                response=FeedbackResponse(
                    accepted=True,
                    counted=existing.is_counted,
                    not_counted_reason=None if existing.is_counted else "duplicate_feedback",
                    familiarity_score=self._current_familiarity(request),
                    replay_job_created=False,
                )
            )

        now = self.clock()
        session_id = request.session_id or query.session_id
        if request.feedback_type in NOT_CONSOLIDATING_FEEDBACK:
            self._log(
                query=query,
                request=request,
                session_id=session_id,
                now=now,
                is_counted=False,
                not_counted_reason="not_consolidating_feedback",
                result_rank=exposure.result_rank,
                final_score=exposure.final_score,
                similarity=exposure.similarity,
            )
            return FeedbackOutcome(
                response=FeedbackResponse(
                    accepted=True,
                    counted=False,
                    not_counted_reason="not_consolidating_feedback",
                    familiarity_score=self._current_familiarity(request),
                    replay_job_created=False,
                )
            )

        counted_exists = self.session.scalar(
            select(MemoryAccessLog.log_id).where(
                MemoryAccessLog.query_id == query.query_id,
                MemoryAccessLog.memory_type == MemoryType(request.memory_type),
                MemoryAccessLog.memory_id == request.memory_id,
                MemoryAccessLog.is_counted.is_(True),
            )
        )
        if counted_exists is not None:
            return self._not_counted(
                request=request,
                query=query,
                session_id=session_id,
                reason="duplicate_feedback",
                now=now,
                exposure=exposure,
            )

        dedup_since = now - timedelta(minutes=self.settings.feedback_dedup_minutes)
        recent = self.session.scalar(
            select(MemoryAccessLog.log_id).where(
                MemoryAccessLog.actor_id == user.subject,
                MemoryAccessLog.session_id == session_id,
                MemoryAccessLog.query_hash == query.query_hash,
                MemoryAccessLog.memory_id == request.memory_id,
                MemoryAccessLog.is_counted.is_(True),
                MemoryAccessLog.created_at >= dedup_since,
            )
        )
        if recent is not None:
            return self._not_counted(
                request=request,
                query=query,
                session_id=session_id,
                reason="recent_duplicate_query",
                now=now,
                exposure=exposure,
            )

        gain_today = self._daily_gain_row(request.memory_type, request.memory_id, now.date())
        gained = gain_today.gained_score if gain_today is not None else 0.0
        gain = clamp_daily_gain(
            self.settings.familiarity_daily_cap - gained, self.settings.familiarity_step
        )
        if gain <= 0:
            return self._not_counted(
                request=request,
                query=query,
                session_id=session_id,
                reason="daily_cap_reached",
                now=now,
                exposure=exposure,
            )

        if request.memory_type == "event":
            return self._apply_event_feedback(
                user=user,
                request=request,
                query=query,
                session_id=session_id,
                exposure=exposure,
                gain_today=gain_today,
                gain=gain,
                now=now,
            )
        return self._apply_wiki_feedback(
            user=user,
            request=request,
            query=query,
            session_id=session_id,
            exposure=exposure,
            gain_today=gain_today,
            gain=gain,
            now=now,
        )

    def _apply_event_feedback(
        self,
        *,
        user: CurrentUser,
        request: FeedbackRequest,
        query: MemoryQuery,
        session_id: UUID | None,
        exposure: MemoryAccessLog,
        gain_today: FamiliarityDailyGain | None,
        gain: float,
        now: datetime,
    ) -> FeedbackOutcome:
        event = self.session.get(EventMemory, request.memory_id, with_for_update=True)
        if event is None:
            raise NotFoundError("事件不存在")
        if not can_view_memory(user, event):
            raise NotFoundError("事件不存在")
        familiarity = materialize_familiarity(
            event.familiarity_score,
            anchor=event.familiarity_updated_at,
            created_at=event.created_at,
            clock=self.clock,
            gamma=self.settings.familiarity_gamma,
            max_score=self.settings.familiarity_max,
            protect_hours=self.settings.new_memory_protect_hours,
        ).score
        previous = familiarity
        event.familiarity_score = min(self.settings.familiarity_max, familiarity + gain)
        event.familiarity_updated_at = now
        event.last_access_time = now
        event.access_count += 1
        self._record_gain(request.memory_type, request.memory_id, gain_today, gained=gain, now=now)
        self.session.add(
            MemoryAccessLog(
                query_id=query.query_id,
                session_id=session_id,
                actor_id=user.subject,
                memory_type=MemoryType.EVENT,
                memory_id=request.memory_id,
                query_hash=query.query_hash,
                result_rank=exposure.result_rank,
                similarity=exposure.similarity,
                final_score=exposure.final_score,
                feedback_type=request.feedback_type,
                is_counted=True,
                access_date=now.date(),
                counted_at=now,
            )
        )
        replay_event_id: UUID | None = None
        replay_before_version: int | None = None
        if (
            previous < self.settings.replay_threshold <= event.familiarity_score
            and event.replay_count < self.settings.replay_max_count
        ):
            job = BackgroundJobService(self.session).create(
                job_type=BackgroundJobType.REPLAY,
                resource_type="event",
                resource_id=event.event_id,
                payload=BackgroundJobPayload(
                    operation="state_replay",
                    event_id=event.event_id,
                    entity_id=event.entity_id,
                ),
                requested_by=user.subject,
            )
            replay_event_id = event.event_id
            replay_before_version = self._entity_state_version(event.entity_id)
            job.payload = {**job.payload, "target_state_version": replay_before_version}
        self.session.flush()
        return FeedbackOutcome(
            response=FeedbackResponse(
                accepted=True,
                counted=True,
                not_counted_reason=None,
                familiarity_score=event.familiarity_score,
                replay_job_created=replay_event_id is not None,
            ),
            replay_event_id=replay_event_id,
            replay_before_state_version=replay_before_version,
        )

    def _apply_wiki_feedback(
        self,
        *,
        user: CurrentUser,
        request: FeedbackRequest,
        query: MemoryQuery,
        session_id: UUID | None,
        exposure: MemoryAccessLog,
        gain_today: FamiliarityDailyGain | None,
        gain: float,
        now: datetime,
    ) -> FeedbackOutcome:
        if not can_view_wiki(user):
            raise NotFoundError("Wiki 分块不存在")
        chunk = self.session.get(WikiChunk, request.memory_id, with_for_update=True)
        if chunk is None:
            raise NotFoundError("Wiki 分块不存在")
        familiarity = materialize_familiarity(
            chunk.familiarity_score,
            anchor=chunk.familiarity_updated_at,
            created_at=chunk.created_at,
            clock=self.clock,
            gamma=self.settings.familiarity_gamma,
            max_score=self.settings.familiarity_max,
            protect_hours=self.settings.new_memory_protect_hours,
        ).score
        chunk.familiarity_score = min(self.settings.familiarity_max, familiarity + gain)
        chunk.familiarity_updated_at = now
        chunk.last_access_time = now
        chunk.access_count += 1
        self._record_gain(request.memory_type, request.memory_id, gain_today, gained=gain, now=now)
        self.session.add(
            MemoryAccessLog(
                query_id=query.query_id,
                session_id=session_id,
                actor_id=user.subject,
                memory_type=MemoryType.WIKI,
                memory_id=request.memory_id,
                query_hash=query.query_hash,
                result_rank=exposure.result_rank,
                similarity=exposure.similarity,
                final_score=exposure.final_score,
                feedback_type=request.feedback_type,
                is_counted=True,
                access_date=now.date(),
                counted_at=now,
            )
        )
        self.session.flush()
        return FeedbackOutcome(
            response=FeedbackResponse(
                accepted=True,
                counted=True,
                not_counted_reason=None,
                familiarity_score=chunk.familiarity_score,
                replay_job_created=False,
            )
        )

    def _not_counted(
        self,
        *,
        request: FeedbackRequest,
        query: MemoryQuery,
        session_id: UUID | None,
        reason: str,
        now: datetime,
        exposure: MemoryAccessLog,
    ) -> FeedbackOutcome:
        self._log(
            query=query,
            request=request,
            session_id=session_id,
            now=now,
            is_counted=False,
            not_counted_reason=reason,
            result_rank=exposure.result_rank,
            final_score=exposure.final_score,
            similarity=exposure.similarity,
        )
        return FeedbackOutcome(
            response=FeedbackResponse(
                accepted=True,
                counted=False,
                not_counted_reason=reason,
                familiarity_score=self._current_familiarity(request),
                replay_job_created=False,
            )
        )

    def _log(
        self,
        *,
        query: MemoryQuery,
        request: FeedbackRequest,
        session_id: UUID | None,
        now: datetime,
        is_counted: bool,
        not_counted_reason: str | None,
        result_rank: int,
        final_score: float,
        similarity: float,
    ) -> None:
        self.session.add(
            MemoryAccessLog(
                query_id=query.query_id,
                session_id=session_id,
                actor_id=query.actor_id,
                memory_type=MemoryType(request.memory_type),
                memory_id=request.memory_id,
                query_hash=query.query_hash,
                result_rank=result_rank,
                similarity=similarity,
                final_score=final_score,
                feedback_type=request.feedback_type,
                is_counted=is_counted,
                not_counted_reason=not_counted_reason,
                access_date=now.date(),
                counted_at=now if is_counted else None,
            )
        )
        self.session.flush()

    def _load_query(self, user: CurrentUser, query_id: UUID) -> MemoryQuery:
        query = self.session.get(MemoryQuery, query_id)
        if query is None or (query.actor_id != user.subject and not user.is_admin):
            raise NotFoundError("查询记录不存在")
        return query

    def _daily_gain_row(
        self, memory_type: str, memory_id: UUID, gain_date: date
    ) -> FamiliarityDailyGain | None:
        return self.session.scalar(
            select(FamiliarityDailyGain)
            .where(
                FamiliarityDailyGain.memory_type == MemoryType(memory_type),
                FamiliarityDailyGain.memory_id == memory_id,
                FamiliarityDailyGain.gain_date == gain_date,
            )
            .with_for_update()
        )

    def _record_gain(
        self,
        memory_type: str,
        memory_id: UUID,
        gain_today: FamiliarityDailyGain | None,
        *,
        gained: float,
        now: datetime,
    ) -> None:
        if gain_today is None:
            self.session.add(
                FamiliarityDailyGain(
                    memory_type=MemoryType(memory_type),
                    memory_id=memory_id,
                    gain_date=now.date(),
                    gained_score=gained,
                    updated_at=now,
                )
            )
            return
        gain_today.gained_score = min(
            self.settings.familiarity_daily_cap, gain_today.gained_score + gained
        )
        gain_today.updated_at = now
        self.session.flush()

    def _current_familiarity(self, request: FeedbackRequest) -> float:
        if request.memory_type == "event":
            event = self.session.get(EventMemory, request.memory_id)
            if event is None:
                raise NotFoundError("事件不存在")
            return materialize_familiarity(
                event.familiarity_score,
                anchor=event.familiarity_updated_at,
                created_at=event.created_at,
                clock=self.clock,
                gamma=self.settings.familiarity_gamma,
                max_score=self.settings.familiarity_max,
                protect_hours=self.settings.new_memory_protect_hours,
            ).score
        chunk = self.session.get(WikiChunk, request.memory_id)
        if chunk is None:
            raise NotFoundError("Wiki 分块不存在")
        return materialize_familiarity(
            chunk.familiarity_score,
            anchor=chunk.familiarity_updated_at,
            created_at=chunk.created_at,
            clock=self.clock,
            gamma=self.settings.familiarity_gamma,
            max_score=self.settings.familiarity_max,
            protect_hours=self.settings.new_memory_protect_hours,
        ).score

    def _entity_state_version(self, entity_id: UUID) -> int:
        from app.models import Entity

        entity = self.session.get(Entity, entity_id)
        return entity.current_state_version if entity is not None else 0
