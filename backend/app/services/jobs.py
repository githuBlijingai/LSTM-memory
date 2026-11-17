from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.logging import redact_text
from app.models import BackgroundJob, BackgroundJobType, JobStatus
from app.schemas.json_fields import BackgroundJobPayload, to_json_dict


class BackgroundJobService:
    def __init__(
        self, session: Session, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.session = session
        self.clock = clock

    def create(
        self,
        *,
        job_type: BackgroundJobType,
        resource_type: str,
        resource_id: UUID,
        payload: BackgroundJobPayload,
        requested_by: str,
    ) -> BackgroundJob:
        job = BackgroundJob(
            job_type=job_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=to_json_dict(payload),
            requested_by=requested_by,
            status=JobStatus.QUEUED,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get(self, job_id: UUID, *, for_update: bool = False) -> BackgroundJob:
        statement = select(BackgroundJob).where(BackgroundJob.job_id == job_id)
        if for_update:
            statement = statement.with_for_update()
        job = self.session.scalar(statement)
        if job is None:
            raise NotFoundError("任务不存在")
        return job

    def start(self, job: BackgroundJob) -> None:
        if job.status not in {JobStatus.QUEUED, JobStatus.RETRY_WAIT}:
            raise AppError("TASK_CONFLICT", "只有排队任务可以开始", 409)
        job.status = JobStatus.RUNNING
        job.started_at = self.clock()
        job.finished_at = None
        job.attempt_count += 1
        self.session.flush()

    def set_progress(self, job: BackgroundJob, progress: float) -> None:
        if job.status != JobStatus.RUNNING:
            raise AppError("TASK_CONFLICT", "只有运行中任务可以更新进度", 409)
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        job.progress = progress
        self.session.flush()

    def mark_succeeded(self, job: BackgroundJob) -> None:
        if job.status != JobStatus.RUNNING:
            raise AppError("TASK_CONFLICT", "只有运行中任务可以完成", 409)
        job.status = JobStatus.SUCCEEDED
        job.progress = 1.0
        job.finished_at = self.clock()
        self.session.flush()

    def mark_failed(self, job: BackgroundJob, error: str, *, dead_letter: bool = False) -> None:
        job.status = JobStatus.DEAD_LETTER if dead_letter else JobStatus.FAILED
        job.last_error = redact_text(error)[:1000]
        job.finished_at = self.clock()
        self.session.flush()

    def retry(self, job: BackgroundJob) -> None:
        if job.status not in {JobStatus.FAILED, JobStatus.DEAD_LETTER}:
            raise AppError("TASK_CONFLICT", "只有失败任务可以重试", 409)
        job.status = JobStatus.QUEUED
        job.progress = 0.0
        job.last_error = None
        job.started_at = None
        job.finished_at = None
        self.session.flush()
