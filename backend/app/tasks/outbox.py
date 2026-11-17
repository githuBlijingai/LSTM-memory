"""Outbox 派发任务：把业务事件转交给 Celery 任务。"""

from __future__ import annotations

import structlog

from app.core.config import get_settings
from app.core.metrics import record_outbox_dispatched
from app.models import OutboxEvent
from app.services.outbox import OutboxService
from app.tasks.support import require_session_factory
from app.worker import celery_app

logger = structlog.get_logger(__name__)

OUTBOX_BATCH_SIZE = 100
OUTBOX_MAX_ATTEMPTS = 5


def _dispatch(event: OutboxEvent) -> None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "memory.event.created":
        from app.tasks.event_pipeline import process_event

        process_event.delay(str(payload["event_id"]))
        return
    if event.event_type == "memory.state.rebuild.requested":
        from app.tasks.rebuild import state_rebuild

        state_rebuild.delay(str(payload["entity_id"]), str(payload["job_id"]))
        return
    if event.event_type == "wiki.sync.requested":
        from app.tasks.wiki_sync import sync_wiki

        sync_wiki.delay(str(payload["resource_id"]), str(payload.get("job_id") or ""))
        return
    raise ValueError(f"未注册的 Outbox 事件类型: {event.event_type}")


@celery_app.task(name="app.tasks.outbox.dispatch_outbox")
def dispatch_outbox(limit: int = OUTBOX_BATCH_SIZE) -> dict[str, object]:
    settings = get_settings()
    factory = require_session_factory(settings)
    sent = 0
    failed = 0
    with factory() as session:
        service = OutboxService(session)
        events = service.claim_batch(limit=limit)
        session.commit()
        for event in events:
            try:
                _dispatch(event)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                service.mark_failed(event, str(exc), max_attempts=OUTBOX_MAX_ATTEMPTS)
                session.commit()
                failed += 1
                record_outbox_dispatched("failed")
                logger.warning(
                    "outbox_dispatch_failed",
                    outbox_id=str(event.outbox_id),
                    event_type=event.event_type,
                    error=str(exc)[:200],
                )
            else:
                service.mark_processed(event)
                session.commit()
                sent += 1
                record_outbox_dispatched("sent")
    if sent or failed:
        logger.info("outbox_dispatch_completed", sent=sent, failed=failed)
    return {"sent": sent, "failed": failed}
