from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
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

from app.models.base import Base, CreatedAtMixin, pg_enum
from app.models.enums import FeedbackType, MemoryType


class MemoryQuery(CreatedAtMixin, Base):
    __tablename__ = "memory_query"

    query_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("working_session.session_id", ondelete="SET NULL")
    )
    query_text_redacted: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(CHAR(64))
    intent: Mapped[str] = mapped_column(String(32))
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    event_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    wiki_candidate_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        CheckConstraint("intent IN ('event', 'wiki', 'mixed')", name="intent_valid"),
        CheckConstraint("event_candidate_count >= 0", name="event_candidates_nonnegative"),
        CheckConstraint("wiki_candidate_count >= 0", name="wiki_candidates_nonnegative"),
    )


class MemoryAccessLog(CreatedAtMixin, Base):
    __tablename__ = "memory_access_log"

    log_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    query_id: Mapped[UUID] = mapped_column(ForeignKey("memory_query.query_id", ondelete="CASCADE"))
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("working_session.session_id", ondelete="SET NULL")
    )
    actor_id: Mapped[str] = mapped_column(String(128))
    memory_type: Mapped[MemoryType] = mapped_column(pg_enum(MemoryType, "memory_type"))
    memory_id: Mapped[UUID]
    query_hash: Mapped[str] = mapped_column(CHAR(64))
    result_rank: Mapped[int] = mapped_column(Integer)
    similarity: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float)
    feedback_type: Mapped[FeedbackType] = mapped_column(pg_enum(FeedbackType, "feedback_type"))
    is_counted: Mapped[bool] = mapped_column(Boolean, default=False)
    not_counted_reason: Mapped[str | None] = mapped_column(String(64))
    access_date: Mapped[date] = mapped_column(Date)
    counted_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint(
            "query_id", "memory_type", "memory_id", "feedback_type", name="access_feedback_once"
        ),
        Index(
            "uq_access_counted_result",
            "query_id",
            "memory_type",
            "memory_id",
            unique=True,
            postgresql_where=is_counted.is_(True),
        ),
        Index(
            "ix_access_feedback_dedup",
            "actor_id",
            "session_id",
            "query_hash",
            "memory_id",
            "is_counted",
            "created_at",
        ),
        CheckConstraint("result_rank >= 0", name="result_rank_nonnegative"),
    )


class FamiliarityDailyGain(Base):
    __tablename__ = "familiarity_daily_gain"

    memory_type: Mapped[MemoryType] = mapped_column(
        pg_enum(MemoryType, "memory_type"), primary_key=True
    )
    memory_id: Mapped[UUID] = mapped_column(primary_key=True)
    gain_date: Mapped[date] = mapped_column(Date, primary_key=True)
    gained_score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("gained_score >= 0 AND gained_score <= 0.3", name="daily_gain_range"),
    )
