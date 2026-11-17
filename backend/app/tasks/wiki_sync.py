from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.logging import redact_text
from app.ml.registry import get_model_registry
from app.models import BackgroundJobType, JobStatus, WikiSyncTask
from app.services.wiki_sync import WikiSyncService
from app.tasks.support import (
    finish_job,
    record_job_retry,
    require_session_factory,
    retry_countdown,
    start_existing_job,
    start_job_for_resource,
)
from app.worker import celery_app

logger = structlog.get_logger(__name__)

WIKI_SYNC_MAX_RETRIES = 3


@celery_app.task(
    bind=True,
    name="app.tasks.wiki_sync.sync_wiki",
    max_retries=WIKI_SYNC_MAX_RETRIES,
)
def sync_wiki(self, task_id: str, job_id: str | None = None) -> dict[str, object]:
    settings = get_settings()
    factory = require_session_factory(settings)
    task_uuid = UUID(task_id)
    if job_id:
        handle = start_existing_job(factory, UUID(job_id))
    else:
        handle = start_job_for_resource(
            factory,
            job_type=BackgroundJobType.WIKI_SYNC,
            resource_type="wiki_sync_task",
            resource_id=task_uuid,
            operation="wiki_sync",
        )
    if not handle.active:
        return {"status": "skipped", "reason": "job_terminal"}
    try:
        models = get_model_registry(settings).get_bundle()
        service = WikiSyncService(factory, settings, models.embedding)
        outcome = service.process_task(task_uuid)
    except Exception as exc:
        retries = self.request.retries
        if retries >= self.max_retries:
            _mark_task_status(factory, task_uuid, JobStatus.DEAD_LETTER, exc)
            finish_job(factory, handle.job_id, success=False, error=exc, dead_letter=True)
            logger.error(
                "wiki_sync_dead_letter",
                task_id=task_id,
                retries=retries,
                error_type=type(exc).__name__,
            )
            raise
        _mark_task_status(factory, task_uuid, JobStatus.RETRY_WAIT, exc)
        record_job_retry(factory, handle.job_id, exc)
        raise self.retry(exc=exc, countdown=retry_countdown(retries)) from exc
    finish_job(factory, handle.job_id, success=True)
    return {
        "status": outcome.status,
        "task_id": str(outcome.task_id),
        "processed_count": outcome.processed_count,
        "success_count": outcome.success_count,
        "failed_count": outcome.failed_count,
        "cursor": outcome.cursor,
    }


def _mark_task_status(
    factory, task_id: UUID, status: JobStatus, error: Exception | None = None
) -> None:
    with factory() as session:
        task = session.get(WikiSyncTask, task_id)
        if task is None:
            return
        task.status = status
        if status == JobStatus.DEAD_LETTER:
            task.finished_at = datetime.now(UTC)
        if error is not None:
            items = list(task.error_sample or [])
            if len(items) < 20:
                items.append(
                    {
                        "code": type(error).__name__[:64],
                        "message": redact_text(str(error))[:256],
                    }
                )
                task.error_sample = items
        session.commit()
