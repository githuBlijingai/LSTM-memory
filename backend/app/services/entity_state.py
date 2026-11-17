from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import redis
import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import redact_text
from app.domain.sequence import left_padded_mask, pad_window_vectors
from app.domain.state import (
    ReplayTransition,
    SnapshotRef,
    can_replay,
    next_state_version,
    plan_replay,
    select_rebuild_snapshot,
)
from app.ml.embedding import EmbeddingProvider
from app.ml.event_encoder import EventEncoder
from app.ml.serialization import (
    SerializedState,
    deserialize_state,
    verify_serialized_state,
)
from app.models import (
    Entity,
    EntityStateSnapshot,
    EntityStatus,
    EventMemory,
    EventStatus,
    ReplayStatus,
    SnapshotKind,
    StateReplayLog,
)

logger = structlog.get_logger(__name__)

CACHE_KEY_PREFIX = "lstm:state"
TWO_POW_63 = 2**63


def _lock_key_from_text(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % TWO_POW_63


def entity_lock_key(entity_id: UUID) -> int:
    return _lock_key_from_text(str(entity_id))


def name_lock_key(owner_id: str, entity_name: str, entity_type: str) -> int:
    return _lock_key_from_text(f"{owner_id}\x00{entity_name}\x00{entity_type}")


def acquire_entity_lock(session: Session, entity_id: UUID) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": entity_lock_key(entity_id)}
    )


def acquire_name_lock(
    session: Session, *, owner_id: str, entity_name: str, entity_type: str
) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": name_lock_key(owner_id, entity_name, entity_type)},
    )


@dataclass(frozen=True)
class TensorState:
    state_version: int
    last_sequence_no: int
    last_event_id: UUID | None
    model_version: str
    serialized: SerializedState


@dataclass(frozen=True)
class StateTransition:
    state: TensorState
    event_vector: list[float]


@dataclass(frozen=True)
class ReplayOutcome:
    log: StateReplayLog
    snapshot: EntityStateSnapshot | None


class EntityStateService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        encoder: EventEncoder,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        decode_state: Callable[[SerializedState], tuple[Any, Any]] = deserialize_state,
        cache: StateCache | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.encoder = encoder
        self.clock = clock
        self._decode_state = decode_state
        self.cache = cache if cache is not None else StateCache(settings.redis_url)

    # ---------- 纯计算 ----------

    def apply_transition(
        self,
        *,
        previous: TensorState | None,
        base_vectors: list[list[float]],
        mask: list[float],
        init_state: tuple[Any, Any] | None,
        state_version: int,
        last_sequence_no: int,
        last_event_id: UUID | None,
        model_version: str,
        snapshot_kind: SnapshotKind,
    ) -> StateTransition:
        if previous is not None:
            if previous.model_version != model_version:
                raise AppError("STATE_MODEL_MISMATCH", "状态模型版本不一致", 409)
            if state_version <= previous.state_version:
                raise AppError("STATE_VERSION_CONFLICT", "状态版本必须单调递增", 409)
            if snapshot_kind == SnapshotKind.REPLAY and (
                last_sequence_no != previous.last_sequence_no
            ):
                raise AppError("REPLAY_INVALID", "重放不得改变 last_sequence_no", 409)
        encoded = self.encoder.encode(base_vectors, mask=mask, init_state=init_state)
        state = TensorState(
            state_version=state_version,
            last_sequence_no=last_sequence_no,
            last_event_id=last_event_id,
            model_version=model_version,
            serialized=encoded.state,
        )
        return StateTransition(state=state, event_vector=encoded.event_vector)

    def decode(self, serialized: SerializedState) -> tuple[Any, Any]:
        return self._decode_state(serialized)

    # ---------- 快照读写 ----------

    def write_snapshot(
        self, entity_id: UUID, state: TensorState, *, snapshot_kind: SnapshotKind
    ) -> EntityStateSnapshot:
        snapshot = EntityStateSnapshot(
            entity_id=entity_id,
            state_version=state.state_version,
            model_version=state.model_version,
            last_event_id=state.last_event_id,
            last_sequence_no=state.last_sequence_no,
            h_tensor=state.serialized.h_bytes,
            c_tensor=state.serialized.c_bytes,
            tensor_dtype=state.serialized.dtype,
            tensor_shape=state.serialized.shape,
            checksum=state.serialized.checksum,
            snapshot_kind=snapshot_kind,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def latest_snapshot(
        self, entity_id: UUID, *, state_version: int | None = None
    ) -> EntityStateSnapshot | None:
        statement = select(EntityStateSnapshot).where(EntityStateSnapshot.entity_id == entity_id)
        if state_version is not None:
            statement = statement.where(EntityStateSnapshot.state_version == state_version)
        statement = statement.order_by(EntityStateSnapshot.state_version.desc()).limit(1)
        return self.session.scalar(statement)

    def snapshot_to_state(self, snapshot: EntityStateSnapshot) -> TensorState:
        verify_serialized_state(
            snapshot.h_tensor,
            snapshot.c_tensor,
            checksum=snapshot.checksum,
            shape=list(snapshot.tensor_shape),
            dtype=snapshot.tensor_dtype,
        )
        return TensorState(
            state_version=snapshot.state_version,
            last_sequence_no=snapshot.last_sequence_no,
            last_event_id=snapshot.last_event_id,
            model_version=snapshot.model_version,
            serialized=SerializedState(
                h_bytes=snapshot.h_tensor,
                c_bytes=snapshot.c_tensor,
                dtype=snapshot.tensor_dtype,
                shape=list(snapshot.tensor_shape),
                checksum=snapshot.checksum,
            ),
        )

    def load_authoritative_state(
        self, entity_id: UUID, *, expected_model_version: str
    ) -> TensorState | None:
        snapshot = self.latest_snapshot(entity_id)
        if snapshot is None:
            return None
        if snapshot.model_version != expected_model_version:
            raise AppError("STATE_MODEL_MISMATCH", "快照模型版本与当前模型不一致", 409)
        return self.snapshot_to_state(snapshot)

    def read_state_with_cache(
        self, entity: Entity, *, expected_model_version: str
    ) -> TensorState | None:
        """优先读取 Redis；miss 或缓存损坏时回退 PostgreSQL。"""
        cached = self.cache.get(entity.entity_id, entity.current_state_version)
        if cached is not None:
            try:
                verify_serialized_state(
                    cached.h_bytes,
                    cached.c_bytes,
                    checksum=cached.checksum,
                    shape=cached.shape,
                    dtype=cached.dtype,
                )
            except AppError:
                logger.warning(
                    "state_cache_invalid",
                    entity_id=str(entity.entity_id),
                    state_version=entity.current_state_version,
                )
            else:
                if cached.model_version == expected_model_version:
                    return TensorState(
                        state_version=cached.state_version,
                        last_sequence_no=cached.last_sequence_no,
                        last_event_id=cached.last_event_id,
                        model_version=cached.model_version,
                        serialized=cached,
                    )
        return self.load_authoritative_state(
            entity.entity_id, expected_model_version=expected_model_version
        )

    # ---------- 状态推进 / 重放 / 重建 ----------

    def advance(
        self,
        entity: Entity,
        *,
        base_vectors: list[list[float]],
        mask: list[float],
        init_state: tuple[Any, Any] | None,
        previous: TensorState | None,
        last_event_id: UUID,
        last_sequence_no: int,
        model_version: str,
        expected_state_version: int | None = None,
    ) -> tuple[EntityStateSnapshot, StateTransition]:
        acquire_entity_lock(self.session, entity.entity_id)
        self.session.refresh(entity)
        if expected_state_version is not None and (
            entity.current_state_version != expected_state_version
        ):
            raise AppError("STATE_VERSION_CONFLICT", "实体状态版本已被其他事务推进", 409)
        version = next_state_version(entity.current_state_version)
        transition = self.apply_transition(
            previous=previous,
            base_vectors=base_vectors,
            mask=mask,
            init_state=init_state,
            state_version=version,
            last_sequence_no=last_sequence_no,
            last_event_id=last_event_id,
            model_version=model_version,
            snapshot_kind=SnapshotKind.UPDATE,
        )
        snapshot = self.write_snapshot(
            entity.entity_id, transition.state, snapshot_kind=SnapshotKind.UPDATE
        )
        entity.current_state_version = version
        entity.current_model_version = model_version
        entity.last_sequence_no = last_sequence_no
        entity.last_event_id = last_event_id
        entity.last_update_time = self.clock()
        self.session.flush()
        return snapshot, transition

    def replay(
        self,
        entity: Entity,
        event: EventMemory,
        *,
        base_vectors: list[list[float]],
        mask: list[float],
        previous: TensorState | None,
        init_state: tuple[Any, Any] | None,
        before_state_version: int,
        model_version: str,
        reason: str = "familiarity_threshold",
    ) -> ReplayOutcome:
        acquire_entity_lock(self.session, entity.entity_id)
        self.session.refresh(entity)
        existing = self.session.scalar(
            select(StateReplayLog)
            .where(
                StateReplayLog.event_id == event.event_id,
                StateReplayLog.before_state_version == before_state_version,
                StateReplayLog.status.in_(
                    [ReplayStatus.QUEUED, ReplayStatus.RUNNING, ReplayStatus.SUCCEEDED]
                ),
            )
            .limit(1)
        )
        if existing is not None:
            return ReplayOutcome(log=existing, snapshot=None)
        if entity.status == EntityStatus.REBUILDING:
            raise AppError("ENTITY_REBUILDING", "实体正在重建，重放稍后执行", 409)
        if entity.current_state_version != before_state_version:
            raise AppError("STATE_VERSION_CONFLICT", "重放目标状态版本已变化", 409)
        if event.status != EventStatus.ACTIVE:
            raise AppError("EVENT_NOT_ACTIVE", "仅 active 事件可以重放", 409)
        if previous is None:
            raise AppError("STATE_UNAVAILABLE", "重放需要权威状态快照", 409)
        if not can_replay(event.replay_count, max_count=self.settings.replay_max_count):
            raise AppError("REPLAY_LIMIT_REACHED", "事件重放次数已达上限", 409)
        if previous is not None and previous.state_version != before_state_version:
            raise AppError("STATE_VERSION_CONFLICT", "重放初始状态版本不匹配", 409)
        plan: ReplayTransition = plan_replay(
            current_state_version=entity.current_state_version,
            last_sequence_no=entity.last_sequence_no,
            replay_count=event.replay_count,
            max_count=self.settings.replay_max_count,
        )
        transition = self.apply_transition(
            previous=previous,
            base_vectors=base_vectors,
            mask=mask,
            init_state=init_state,
            state_version=plan.state_version,
            last_sequence_no=plan.last_sequence_no,
            last_event_id=entity.last_event_id,
            model_version=model_version,
            snapshot_kind=SnapshotKind.REPLAY,
        )
        snapshot = self.write_snapshot(
            entity.entity_id, transition.state, snapshot_kind=SnapshotKind.REPLAY
        )
        now = self.clock()
        log = StateReplayLog(
            entity_id=entity.entity_id,
            event_id=event.event_id,
            reason=reason,
            before_state_version=before_state_version,
            after_state_version=plan.state_version,
            model_version=model_version,
            status=ReplayStatus.SUCCEEDED,
            attempt_count=1,
            completed_at=now,
        )
        self.session.add(log)
        event.replay_count += 1
        entity.current_state_version = plan.state_version
        entity.current_model_version = model_version
        entity.last_update_time = now
        self.session.flush()
        return ReplayOutcome(log=log, snapshot=snapshot)

    def rebuild(
        self,
        entity: Entity,
        *,
        change_sequence_no: int,
        model_version: str,
        embedding: EmbeddingProvider,
        target_job_id: UUID | None = None,
    ) -> EntityStateSnapshot | None:
        acquire_entity_lock(self.session, entity.entity_id)
        self.session.refresh(entity)
        entity.status = EntityStatus.REBUILDING
        entity.last_update_time = self.clock()
        self.session.flush()

        snapshots = list(
            self.session.scalars(
                select(EntityStateSnapshot)
                .where(EntityStateSnapshot.entity_id == entity.entity_id)
                .order_by(EntityStateSnapshot.state_version.desc())
            ).all()
        )
        references = [
            SnapshotRef(
                snapshot_id=item.snapshot_id,
                state_version=item.state_version,
                model_version=item.model_version,
                last_sequence_no=item.last_sequence_no,
            )
            for item in snapshots
        ]
        selected = select_rebuild_snapshot(
            references, model_version=model_version, change_sequence_no=change_sequence_no
        )
        previous: TensorState | None = None
        init_state: tuple[Any, Any] | None = None
        start_sequence = 0
        if selected is not None:
            selected_snapshot = next(
                item for item in snapshots if item.snapshot_id == selected.snapshot_id
            )
            previous = self.snapshot_to_state(selected_snapshot)
            init_state = self.decode(previous.serialized)
            start_sequence = previous.last_sequence_no

        events = list(
            self.session.scalars(
                select(EventMemory)
                .where(
                    EventMemory.entity_id == entity.entity_id,
                    EventMemory.status == EventStatus.ACTIVE,
                    EventMemory.deleted_at.is_(None),
                    EventMemory.sequence_no.is_not(None),
                    EventMemory.sequence_no > start_sequence,
                )
                .order_by(EventMemory.event_time, EventMemory.event_id)
            ).all()
        )
        if not events and previous is None:
            raise AppError("REBUILD_NO_EVENTS", "没有可用于重建的有效事件", 409)

        only_events = [event for event in events if event.sequence_no is not None]
        if previous is not None and not only_events:
            # 快照之后没有新事件：生成新版本快照，避免与既有版本冲突。
            previous = TensorState(
                state_version=next_state_version(
                    max(entity.current_state_version, previous.state_version)
                ),
                last_sequence_no=previous.last_sequence_no,
                last_event_id=previous.last_event_id,
                model_version=previous.model_version,
                serialized=previous.serialized,
            )
        texts = [(event.raw_content or event.title or "") for event in only_events]
        base_vectors = embedding.embed(texts) if texts else []
        version = (
            max(entity.current_state_version, previous.state_version)
            if previous is not None
            else entity.current_state_version
        )
        last_event_time = entity.last_event_time
        for index, event in enumerate(only_events):
            lower = max(0, index - self.settings.event_window_size + 1)
            window = base_vectors[lower : index + 1]
            window_size = self.settings.event_window_size
            mask = list(left_padded_mask(len(window), window_size))
            version = next_state_version(version)
            transition = self.apply_transition(
                previous=previous,
                base_vectors=pad_window_vectors(window, window_size=window_size),
                mask=mask,
                init_state=init_state,
                state_version=version,
                last_sequence_no=event.sequence_no or 0,
                last_event_id=event.event_id,
                model_version=model_version,
                snapshot_kind=SnapshotKind.REBUILD,
            )
            event.embedding = transition.event_vector
            event.event_model_version = model_version
            previous = transition.state
            init_state = self.decode(previous.serialized)
            if last_event_time is None or event.event_time > last_event_time:
                last_event_time = event.event_time

        snapshot = None
        if previous is not None:
            snapshot = self.write_snapshot(
                entity.entity_id, previous, snapshot_kind=SnapshotKind.REBUILD
            )
            entity.current_state_version = previous.state_version
            entity.current_model_version = model_version
            entity.last_sequence_no = previous.last_sequence_no
            entity.last_event_id = previous.last_event_id
        entity.last_event_time = last_event_time
        entity.status = EntityStatus.ACTIVE
        entity.last_update_time = self.clock()
        self.session.flush()
        logger.info(
            "entity_rebuilt",
            entity_id=str(entity.entity_id),
            target_job_id=str(target_job_id) if target_job_id else None,
            replayed_events=len(only_events),
            state_version=entity.current_state_version,
        )
        return snapshot

    # ---------- 缓存 ----------

    def sync_cache(self, snapshot: EntityStateSnapshot) -> bool:
        return self.cache.set_snapshot(snapshot)


@dataclass(frozen=True)
class CachedState:
    state_version: int
    last_sequence_no: int
    last_event_id: UUID | None
    model_version: str
    h_bytes: bytes
    c_bytes: bytes
    dtype: str
    shape: list[int]
    checksum: str


class StateCache:
    """Redis 状态缓存；失败只记录指标，不回滚权威状态。"""

    def __init__(self, redis_url: str | None, *, client: Any | None = None) -> None:
        self.redis_url = redis_url
        self._client = client

    def _client_or_none(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.redis_url:
            return None
        try:
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=False)
        except (ValueError, redis.RedisError) as exc:
            logger.warning("state_cache_client_invalid", error=redact_text(str(exc)))
            return None
        return self._client

    @staticmethod
    def key(entity_id: UUID, state_version: int) -> str:
        return f"{CACHE_KEY_PREFIX}:{entity_id}:{state_version}"

    def set_snapshot(self, snapshot: EntityStateSnapshot) -> bool:
        client = self._client_or_none()
        if client is None:
            return False
        try:
            client.set(
                self.key(snapshot.entity_id, snapshot.state_version),
                _encode_cache_payload(snapshot),
            )
            return True
        except (redis.RedisError, OSError) as exc:
            logger.warning(
                "state_cache_write_failed",
                entity_id=str(snapshot.entity_id),
                state_version=snapshot.state_version,
                error=redact_text(str(exc)),
            )
            return False

    def get(self, entity_id: UUID, state_version: int) -> CachedState | None:
        client = self._client_or_none()
        if client is None:
            return None
        try:
            payload = client.get(self.key(entity_id, state_version))
        except (redis.RedisError, OSError) as exc:
            logger.warning(
                "state_cache_read_failed",
                entity_id=str(entity_id),
                state_version=state_version,
                error=redact_text(str(exc)),
            )
            return None
        if payload is None:
            return None
        try:
            return _decode_cache_payload(payload)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, AppError) as exc:
            logger.warning(
                "state_cache_payload_invalid",
                entity_id=str(entity_id),
                state_version=state_version,
                error=redact_text(str(exc)),
            )
            return None


def _encode_cache_payload(snapshot: EntityStateSnapshot) -> bytes:
    return json.dumps(
        {
            "state_version": snapshot.state_version,
            "last_sequence_no": snapshot.last_sequence_no,
            "last_event_id": str(snapshot.last_event_id) if snapshot.last_event_id else None,
            "model_version": snapshot.model_version,
            "h": base64.b64encode(snapshot.h_tensor).decode("ascii"),
            "c": base64.b64encode(snapshot.c_tensor).decode("ascii"),
            "dtype": snapshot.tensor_dtype,
            "shape": list(snapshot.tensor_shape),
            "checksum": snapshot.checksum,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_cache_payload(payload: bytes) -> CachedState:
    value = json.loads(payload)
    h_bytes = base64.b64decode(value["h"], validate=True)
    c_bytes = base64.b64decode(value["c"], validate=True)
    verify_serialized_state(
        h_bytes,
        c_bytes,
        checksum=value["checksum"],
        shape=list(value["shape"]),
        dtype=value["dtype"],
    )
    return CachedState(
        state_version=int(value["state_version"]),
        last_sequence_no=int(value["last_sequence_no"]),
        last_event_id=UUID(value["last_event_id"]) if value["last_event_id"] else None,
        model_version=value["model_version"],
        h_bytes=h_bytes,
        c_bytes=c_bytes,
        dtype=value["dtype"],
        shape=list(value["shape"]),
        checksum=value["checksum"],
    )


def require_entity(session: Session, entity_id: UUID) -> Entity:
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise NotFoundError("实体不存在")
    return entity
