from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, pg_enum
from app.models.enums import EntityStatus, EventStatus, Visibility


class Entity(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "entity"

    entity_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_name: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    visibility: Mapped[Visibility] = mapped_column(
        pg_enum(Visibility, "visibility"), default=Visibility.PRIVATE
    )
    status: Mapped[EntityStatus] = mapped_column(
        pg_enum(EntityStatus, "entity_status"), default=EntityStatus.ACTIVE
    )
    current_state_version: Mapped[int] = mapped_column(BigInteger, default=0)
    current_model_version: Mapped[str | None] = mapped_column(String(128))
    last_sequence_no: Mapped[int] = mapped_column(BigInteger, default=0)
    last_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("event_memory.event_id", ondelete="SET NULL", use_alter=True)
    )
    last_event_time: Mapped[datetime | None]
    readable_summary: Mapped[str] = mapped_column(Text, default="")
    summary_source_event_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    summary_version: Mapped[str | None] = mapped_column(String(64))
    summary_updated_at: Mapped[datetime | None]
    last_update_time: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("current_state_version >= 0", name="state_version_nonnegative"),
        CheckConstraint("last_sequence_no >= 0", name="last_sequence_nonnegative"),
    )


class EventMemory(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "event_memory"

    event_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entity.entity_id", ondelete="RESTRICT"), index=True
    )
    subject: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    event_time: Mapped[datetime] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(256))
    raw_content: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(256))
    event_model_version: Mapped[str | None] = mapped_column(String(128))
    sequence_no: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(64))
    source_event_key: Mapped[str | None] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    initial_decay_score: Mapped[float] = mapped_column(Float, default=1.0)
    decay_score: Mapped[float] = mapped_column(Float, default=1.0)
    familiarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    familiarity_updated_at: Mapped[datetime | None]
    last_access_time: Mapped[datetime | None]
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[EventStatus] = mapped_column(
        pg_enum(EventStatus, "event_status"), default=EventStatus.PENDING, index=True
    )
    supersedes_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("event_memory.event_id", ondelete="SET NULL")
    )
    visibility: Mapped[Visibility] = mapped_column(
        pg_enum(Visibility, "visibility"), default=Visibility.PRIVATE
    )
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    purge_after: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("entity_id", "sequence_no", name="event_entity_sequence"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        CheckConstraint(
            "familiarity_score >= 0 AND familiarity_score <= 2", name="familiarity_range"
        ),
        CheckConstraint("access_count >= 0", name="access_count_nonnegative"),
        CheckConstraint("replay_count >= 0 AND replay_count <= 2", name="replay_count_range"),
        CheckConstraint(
            "source IN ('api', 'working_memory', 'import', 'correction')", name="source_valid"
        ),
        Index(
            "uq_event_source_event_key_active",
            "owner_id",
            "source",
            "source_event_key",
            unique=True,
            postgresql_where=source_event_key.is_not(None),
        ),
        Index("ix_event_entity_time", "entity_id", "event_time"),
        Index("ix_event_status_created", "status", "created_at"),
        Index("ix_event_owner_visibility", "owner_id", "visibility"),
        Index(
            "ix_event_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
