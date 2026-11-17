from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging import redact_text
from app.db import get_session_factory
from app.models import BackgroundJob, BackgroundJobType, JobStatus
from app.schemas.json_fields import BackgroundJobPayload
from app.services.jobs import BackgroundJobService

TERMINAL_JOB_STATUSES = {JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.DEAD_LETTER}


@dataclass(frozen=True)
class JobHandle:
    job_id: UUID
    active: bool


def require_session_factory(settings: Settings) -> sessionmaker[Session]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return get_session_factory(settings.database_url)


def find_job(
    session: Session, *, job_type: BackgroundJobType, resource_id: UUID
) -> BackgroundJob | None:
    return session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.job_type == job_type,
            BackgroundJob.resource_id == resource_id,
        )
        .order_by(BackgroundJob.created_at.desc())
        .limit(1)
    )


def start_job_for_resource(
    session_factory: sessionmaker[Session],
    *,
    job_type: BackgroundJobType,
    resource_type: str,
    resource_id: UUID,
    operation: str,
    requested_by: str = "system",
    entity_id: UUID | None = None,
    event_id: UUID | None = None,
    target_state_version: int | None = None,
) -> JobHandle:
    with session_factory() as session:
        job = find_job(session, job_type=job_type, resource_id=resource_id)
        if job is None:
            job = BackgroundJobService(session).create(
                job_type=job_type,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=BackgroundJobPayload(
                    operation=operation,
                    entity_id=entity_id,
                    event_id=event_id,
                    target_state_version=target_state_version,
                ),
                requested_by=requested_by,
            )
        if job.status in TERMINAL_JOB_STATUSES:
            return JobHandle(job_id=job.job_id, active=False)
        _start_existing(session, job)
        session.commit()
        return JobHandle(job_id=job.job_id, active=True)


def start_existing_job(session_factory: sessionmaker[Session], job_id: UUID) -> JobHandle:
    with session_factory() as session:
        job = session.get(BackgroundJob, job_id)
        if job is None:
            return JobHandle(job_id=job_id, active=False)
        if job.status in TERMINAL_JOB_STATUSES:
            return JobHandle(job_id=job.job_id, active=False)
        _start_existing(session, job)
        session.commit()
        return JobHandle(job_id=job.job_id, active=True)


def _start_existing(session: Session, job: BackgroundJob) -> None:
    service = BackgroundJobService(session)
    if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
        service.start(job)
    elif job.status == JobStatus.FAILED:
        job.status = JobStatus.QUEUED
        service.start(job)
    elif job.status == JobStatus.RUNNING:
        job.attempt_count += 1
        job.finished_at = None
        session.flush()


def record_job_retry(
    session_factory: sessionmaker[Session], job_id: UUID, error: Exception
) -> None:
    with session_factory() as session:
        job = session.get(BackgroundJob, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        job.status = JobStatus.RETRY_WAIT
        job.last_error = redact_text(str(error))[:1000]
        job.finished_at = None
        session.commit()


def finish_job(
    session_factory: sessionmaker[Session],
    job_id: UUID,
    *,
    success: bool,
    error: Exception | None = None,
    dead_letter: bool = False,
) -> None:
    with session_factory() as session:
        job = session.get(BackgroundJob, job_id)
        if job is None:
            return
        service = BackgroundJobService(session)
        if job.status != JobStatus.RUNNING:
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or datetime.now(UTC)
        if success:
            service.mark_succeeded(job)
        else:
            service.mark_failed(job, str(error or "unknown error"), dead_letter=dead_letter)
        session.commit()


def retry_countdown(retries: int, *, base_seconds: int = 30, max_seconds: int = 1800) -> int:
    return min(base_seconds * (2**retries), max_seconds)
