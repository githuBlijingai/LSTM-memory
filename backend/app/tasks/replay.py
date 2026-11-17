from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.ml.registry import get_model_registry
from app.models import BackgroundJobType, Entity, EventMemory
from app.services.entity_state import EntityStateService
from app.tasks.support import (
    finish_job,
    record_job_retry,
    require_session_factory,
    retry_countdown,
    start_job_for_resource,
)
from app.worker import celery_app

logger = structlog.get_logger(__name__)

REPLAY_MAX_RETRIES = 3


@celery_app.task(
    bind=True,
    name="app.tasks.replay.state_replay",
    max_retries=REPLAY_MAX_RETRIES,
)
def state_replay(
    self,
    event_id: str,
    before_state_version: int,
    reason: str = "familiarity_threshold",
) -> dict[str, object]:
    settings = get_settings()
    factory = require_session_factory(settings)
    event_uuid = UUID(event_id)
    handle = start_job_for_resource(
        factory,
        job_type=BackgroundJobType.REPLAY,
        resource_type="event",
        resource_id=event_uuid,
        operation="state_replay",
        event_id=event_uuid,
        target_state_version=before_state_version,
    )
    if not handle.active:
        return {"status": "skipped", "reason": "job_terminal"}
    snapshot = None
    service = None
    try:
        models = get_model_registry(settings).get_bundle()
        with factory() as session:
            event = session.get(EventMemory, event_uuid)
            if event is None:
                raise NotFoundError("事件不存在")
            entity = session.get(Entity, event.entity_id)
            if entity is None:
                raise NotFoundError("实体不存在")
            service = EntityStateService(
                session,
                settings,
                encoder=models.event_encoder,
                clock=lambda: datetime.now(UTC),
            )
            previous = service.load_authoritative_state(
                entity.entity_id, expected_model_version=models.event_model_version
            )
            init_state = service.decode(previous.serialized) if previous else None
            texts = [event.raw_content or event.title or ""]
            base_vectors = models.embedding.embed(texts)
            outcome = service.replay(
                entity,
                event,
                base_vectors=base_vectors,
                mask=[1.0],
                previous=previous,
                init_state=init_state,
                before_state_version=before_state_version,
                model_version=models.event_model_version,
                reason=reason,
            )
            session.commit()
            snapshot = outcome.snapshot
            after_version = outcome.log.after_state_version
    except Exception as exc:
        retries = self.request.retries
        if retries >= self.max_retries:
            finish_job(factory, handle.job_id, success=False, error=exc, dead_letter=True)
            logger.error(
                "state_replay_dead_letter",
                event_id=event_id,
                retries=retries,
                error_type=type(exc).__name__,
            )
            raise
        record_job_retry(factory, handle.job_id, exc)
        raise self.retry(
            exc=exc,
            countdown=retry_countdown(retries),
        ) from exc
    if service is not None and snapshot is not None:
        service.sync_cache(snapshot)
    finish_job(factory, handle.job_id, success=True)
    return {
        "status": "succeeded",
        "event_id": event_id,
        "before_state_version": before_state_version,
        "after_state_version": after_version,
    }
