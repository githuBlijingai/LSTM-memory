from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, pg_enum
from app.models.enums import WorkingSessionStatus


class WorkingSession(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "working_session"

    session_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[WorkingSessionStatus] = mapped_column(
        pg_enum(WorkingSessionStatus, "working_session_status"), default=WorkingSessionStatus.ACTIVE
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_version: Mapped[str | None] = mapped_column(String(64))
    token_budget: Mapped[int] = mapped_column(Integer, default=8000)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime] = mapped_column(index=True)
    expires_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("token_budget > 0", name="token_budget_positive"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
    )


class WorkingItem(CreatedAtMixin, Base):
    __tablename__ = "working_item"

    item_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("working_session.session_id", ondelete="CASCADE"), index=True
    )
    sequence_no: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    explicit_keep: Mapped[bool] = mapped_column(Boolean, default=False)
    consolidated_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("event_memory.event_id", ondelete="SET NULL", use_alter=True)
    )

    __table_args__ = (
        UniqueConstraint("session_id", "sequence_no", name="working_item_session_sequence"),
        CheckConstraint("sequence_no > 0", name="sequence_no_positive"),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="role_valid"),
        Index("ix_working_item_session_sequence", "session_id", "sequence_no"),
    )
