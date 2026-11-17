from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, pg_enum
from app.models.enums import ReplayStatus, SnapshotKind


class EntityStateSnapshot(CreatedAtMixin, Base):
    __tablename__ = "entity_state_snapshot"

    snapshot_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entity.entity_id", ondelete="RESTRICT"))
    state_version: Mapped[int] = mapped_column(BigInteger)
    model_version: Mapped[str] = mapped_column(String(128))
    last_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("event_memory.event_id", ondelete="SET NULL")
    )
    last_sequence_no: Mapped[int] = mapped_column(BigInteger)
    h_tensor: Mapped[bytes] = mapped_column(LargeBinary)
    c_tensor: Mapped[bytes] = mapped_column(LargeBinary)
    tensor_dtype: Mapped[str] = mapped_column(String(16), default="float32")
    tensor_shape: Mapped[list[int]] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(CHAR(64))
    snapshot_kind: Mapped[SnapshotKind] = mapped_column(pg_enum(SnapshotKind, "snapshot_kind"))

    __table_args__ = (
        UniqueConstraint("entity_id", "state_version", name="snapshot_entity_version"),
        Index(
            "ix_snapshot_entity_model_sequence", "entity_id", "model_version", "last_sequence_no"
        ),
    )


class StateReplayLog(CreatedAtMixin, Base):
    __tablename__ = "state_replay_log"

    replay_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entity.entity_id", ondelete="RESTRICT"))
    event_id: Mapped[UUID] = mapped_column(ForeignKey("event_memory.event_id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(String(64), default="familiarity_threshold")
    before_state_version: Mapped[int] = mapped_column(BigInteger)
    after_state_version: Mapped[int] = mapped_column(BigInteger)
    model_version: Mapped[str] = mapped_column(String(128))
    status: Mapped[ReplayStatus] = mapped_column(
        pg_enum(ReplayStatus, "replay_status"), default=ReplayStatus.QUEUED
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        Index(
            "uq_replay_event_target_active",
            "event_id",
            "after_state_version",
            unique=True,
            postgresql_where=status.in_(
                [
                    ReplayStatus.QUEUED.value,
                    ReplayStatus.RUNNING.value,
                    ReplayStatus.SUCCEEDED.value,
                ]
            ),
        ),
    )
