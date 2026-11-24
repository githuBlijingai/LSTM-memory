from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import get_session_factory
from app.domain.scoring import materialize_familiarity, retrieval_hit_delta
from app.models import (
    EventMemory,
    EventStatus,
    MemoryType,
    RetrievalHitGain,
    WikiChunk,
    WikiChunkStatus,
)
from app.services.uow import UnitOfWork

logger = structlog.get_logger(__name__)


def normalize_query_fingerprint(query: str) -> str:
    """查询指纹：去掉首尾空白、转小写、压缩中间空白后做 SHA-256。"""
    normalized = " ".join(query.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class RetrievalReinforcementService:
    """检索命中后的熟悉度强化。

    设计为检索事务之外的旁路写入：使用独立 Session/UoW，任何失败只记日志。
    两层去重：
    - 最近 N 分钟（默认 60）内同一 (actor, 指纹, 记忆) 只计一次；
    - retrieval_hit_gain 的唯一约束保证同一指纹对同一记忆每天最多一行。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.clock = clock

    def reinforce(
        self,
        *,
        actor_id: str,
        query: str,
        hits: Iterable[tuple[str, UUID]],
    ) -> int:
        deduped: list[tuple[str, UUID]] = []
        seen: set[tuple[str, UUID]] = set()
        for memory_type, memory_id in hits:
            key = (str(memory_type), memory_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            return 0

        fingerprint = normalize_query_fingerprint(query)
        now = self.clock()
        cutoff = now - timedelta(minutes=self.settings.retrieval_hit_dedup_minutes)
        applied = 0

        with UnitOfWork(self.session_factory) as uow:
            session = uow.session
            for memory_type, memory_id in deduped:
                if memory_type not in ("event", "wiki"):
                    continue
                kind = MemoryType(memory_type)
                model = EventMemory if kind is MemoryType.EVENT else WikiChunk
                memory = session.get(model, memory_id, with_for_update=True)
                if memory is None or not self._reinforceable(kind, memory):
                    continue

                current = materialize_familiarity(
                    memory.familiarity_score,
                    anchor=memory.familiarity_updated_at,
                    created_at=memory.created_at,
                    clock=lambda: now,
                    gamma=self.settings.familiarity_gamma,
                    max_score=self.settings.familiarity_max,
                    protect_hours=self.settings.new_memory_protect_hours,
                ).score
                delta = retrieval_hit_delta(current, self.settings.retrieval_hit_familiarity_step)
                if delta <= 0:
                    continue

                recent = session.scalar(
                    select(RetrievalHitGain.gain_id)
                    .where(
                        RetrievalHitGain.actor_id == actor_id,
                        RetrievalHitGain.query_hash == fingerprint,
                        RetrievalHitGain.memory_type == kind,
                        RetrievalHitGain.memory_id == memory_id,
                        RetrievalHitGain.hit_at >= cutoff,
                    )
                    .limit(1)
                )
                if recent is not None:
                    continue

                inserted = session.execute(
                    pg_insert(RetrievalHitGain)
                    .values(
                        actor_id=actor_id,
                        query_hash=fingerprint,
                        memory_type=kind,
                        memory_id=memory_id,
                        gain_date=now.date(),
                        gained_score=delta,
                        hit_at=now,
                    )
                    .on_conflict_do_nothing()
                )
                if inserted.rowcount == 0:
                    continue

                # 命中只温和提升熟悉度：不推进 access_count / last_access_time，
                # 这两个字段仍由有效反馈维护。
                memory.familiarity_score = min(1.0, current + delta)
                memory.familiarity_updated_at = now
                applied += 1
            uow.commit()
        return applied

    @staticmethod
    def _reinforceable(memory_type: MemoryType, memory: EventMemory | WikiChunk) -> bool:
        if memory_type is MemoryType.EVENT:
            return isinstance(memory, EventMemory) and memory.status == EventStatus.ACTIVE
        return isinstance(memory, WikiChunk) and memory.status == WikiChunkStatus.ACTIVE


def reinforce_retrieval_hits(
    *,
    settings: Settings,
    actor_id: str,
    query: str,
    hits: list[tuple[str, UUID]],
) -> int:
    """BackgroundTasks 入口：绝不让旁路写入影响检索响应。"""
    try:
        if not settings.database_url or not hits:
            return 0
        service = RetrievalReinforcementService(
            get_session_factory(settings.database_url), settings
        )
        return service.reinforce(actor_id=actor_id, query=query, hits=hits)
    except Exception:
        logger.warning(
            "retrieval_reinforcement_failed",
            actor_id=actor_id,
            hit_count=len(hits),
            exc_info=True,
        )
        return 0
