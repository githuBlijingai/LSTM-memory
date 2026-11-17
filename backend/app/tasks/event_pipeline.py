from __future__ import annotations

from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.metrics import record_event_processed
from app.ml.registry import get_model_registry
from app.models import BackgroundJobType
from app.services.event_memory import EventPipelineService
from app.tasks.support import (
    finish_job,
    record_job_retry,
    require_session_factory,
    retry_countdown,
    start_job_for_resource,
)
from app.worker import celery_app

logger = structlog.get_logger(__name__)

EVENT_PIPELINE_MAX_RETRIES = 5
EVENT_PIPELINE_BACKOFF_SECONDS = 30
EVENT_PIPELINE_MAX_BACKOFF_SECONDS = 1800


@celery_app.task(
    bind=True,
    name="app.tasks.event_pipeline.process_event",
    max_retries=EVENT_PIPELINE_MAX_RETRIES,
)
def process_event(self, event_id: str) -> dict[str, object]:
    settings = get_settings()
    factory = require_session_factory(settings)
    event_uuid = UUID(event_id)
    handle = start_job_for_resource(
        factory,
        job_type=BackgroundJobType.EVENT_PIPELINE,
        resource_type="event",
        resource_id=event_uuid,
        operation="event_pipeline",
        event_id=event_uuid,
    )
    if not handle.active:
        return {"status": "skipped", "reason": "job_terminal"}
    try:
        models = get_model_registry(settings).get_bundle()
        service = EventPipelineService(factory, settings, models)
        result = service.process_event(event_uuid)
    except Exception as exc:
        retries = self.request.retries
        if retries >= self.max_retries:
            finish_job(factory, handle.job_id, success=False, error=exc, dead_letter=True)
            logger.error(
                "event_pipeline_dead_letter",
                event_id=event_id,
                retries=retries,
                error_type=type(exc).__name__,
            )
            record_event_processed("failed")
            raise
        record_job_retry(factory, handle.job_id, exc)
        logger.warning(
            "event_pipeline_retry",
            event_id=event_id,
            retries=retries,
            error_type=type(exc).__name__,
        )
        raise self.retry(
            exc=exc,
            countdown=retry_countdown(
                retries,
                base_seconds=EVENT_PIPELINE_BACKOFF_SECONDS,
                max_seconds=EVENT_PIPELINE_MAX_BACKOFF_SECONDS,
            ),
        ) from exc
    finish_job(factory, handle.job_id, success=True)
    record_event_processed("succeeded")
    return {
        "status": result.status,
        "event_id": str(result.event_id),
        "state_version": result.state_version,
        "sequence_no": result.sequence_no,
        "rebuild_job_id": str(result.rebuild_job_id) if result.rebuild_job_id else None,
    }
