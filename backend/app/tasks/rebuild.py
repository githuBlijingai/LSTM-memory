from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.ml.registry import get_model_registry
from app.models import BackgroundJob, Entity, EventMemory
from app.services.entity_state import EntityStateService
from app.tasks.support import (
    finish_job,
    record_job_retry,
    require_session_factory,
    retry_countdown,
    start_existing_job,
)
from app.worker import celery_app

logger = structlog.get_logger(__name__)

REBUILD_MAX_RETRIES = 3
FULL_REPLAY_OPERATIONS = {"late_event", "model_upgrade", "missing_snapshot"}


@celery_app.task(
    bind=True,
    name="app.tasks.rebuild.state_rebuild",
    max_retries=REBUILD_MAX_RETRIES,
)
def state_rebuild(self, entity_id: str, target_job_id: str) -> dict[str, object]:
    settings = get_settings()
    factory = require_session_factory(settings)
    entity_uuid = UUID(entity_id)
    job_uuid = UUID(target_job_id)
    handle = start_existing_job(factory, job_uuid)
    if not handle.active:
        return {"status": "skipped", "reason": "job_terminal"}
    snapshot = None
    service = None
    state_version = 0
    replayed = 0
    try:
        models = get_model_registry(settings).get_bundle()
        with factory() as session:
            entity = session.get(Entity, entity_uuid)
            if entity is None:
                raise NotFoundError("实体不存在")
            job_payload = _load_job_payload(session, job_uuid)
            change_sequence_no = _resolve_change_sequence(session, job_payload)
            service = EntityStateService(
                session,
                settings,
                encoder=models.event_encoder,
                clock=lambda: datetime.now(UTC),
            )
            snapshot = service.rebuild(
                entity,
                change_sequence_no=change_sequence_no,
                model_version=models.event_model_version,
                embedding=models.embedding,
                target_job_id=job_uuid,
            )
            if snapshot is not None:
                state_version = snapshot.state_version
                replayed = snapshot.last_sequence_no
            session.commit()
    except Exception as exc:
        retries = self.request.retries
        if retries >= self.max_retries:
            finish_job(factory, handle.job_id, success=False, error=exc, dead_letter=True)
            logger.error(
                "state_rebuild_dead_letter",
                entity_id=entity_id,
                target_job_id=target_job_id,
                retries=retries,
                error_type=type(exc).__name__,
            )
            raise
        record_job_retry(factory, handle.job_id, exc)
        raise self.retry(exc=exc, countdown=retry_countdown(retries)) from exc
    if service is not None and snapshot is not None:
        service.sync_cache(snapshot)
    finish_job(factory, handle.job_id, success=True)
    return {
        "status": "succeeded",
        "entity_id": entity_id,
        "target_job_id": target_job_id,
        "state_version": state_version,
        "last_sequence_no": replayed,
    }


def _load_job_payload(session, job_id: UUID) -> dict:
    job = session.get(BackgroundJob, job_id)
    return job.payload if job is not None and isinstance(job.payload, dict) else {}


def _resolve_change_sequence(session, payload: dict) -> int:
    operation = str(payload.get("operation") or "")
    if operation in FULL_REPLAY_OPERATIONS:
        return 0
    raw_event_id = payload.get("event_id")
    if raw_event_id:
        event = session.get(EventMemory, UUID(str(raw_event_id)))
        if event is not None and event.sequence_no is not None:
            return max(0, event.sequence_no - 1)
    return 0
