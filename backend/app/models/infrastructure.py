from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, pg_enum
from app.models.enums import BackgroundJobType, JobStatus, OutboxStatus


class OutboxEvent(CreatedAtMixin, Base):
    __tablename__ = "outbox_event"

    outbox_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(
        pg_enum(OutboxStatus, "outbox_status"), default=OutboxStatus.PENDING
    )
    available_at: Mapped[datetime]
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_outbox_status_available", "status", "available_at"),
    )


class BackgroundJob(CreatedAtMixin, Base):
    __tablename__ = "background_job"

    job_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_type: Mapped[BackgroundJobType] = mapped_column(
        pg_enum(BackgroundJobType, "background_job_type")
    )
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 1", name="progress_range"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_background_job_resource", "resource_type", "resource_id"),
    )


class IdempotencyRecord(CreatedAtMixin, Base):
    __tablename__ = "idempotency_record"

    key: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(CHAR(64))
    request_path: Mapped[str] = mapped_column(String(512))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    resource_id: Mapped[UUID | None]
    expires_at: Mapped[datetime]

    __table_args__ = (PrimaryKeyConstraint("actor_id", "key", "request_path"),)


class AuditLog(CreatedAtMixin, Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(256))
    before_json: Mapped[dict | list | None] = mapped_column(JSONB)
    after_json: Mapped[dict | list | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    request_id: Mapped[UUID]

    __table_args__ = (
        Index("ix_audit_resource", "resource_type", "resource_id"),
        Index("ix_audit_request", "request_id"),
    )
