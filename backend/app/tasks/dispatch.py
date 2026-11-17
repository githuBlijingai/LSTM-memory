from __future__ import annotations

from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)


def _safe_delay(label: str, task, *args: object) -> None:
    try:
        task.delay(*args)
    except Exception as exc:  # pragma: no cover - Broker 不可用时保持任务排队
        logger.warning("task_dispatch_failed", task=label, error=str(exc)[:200])


def dispatch_event_pipeline(event_id: UUID) -> None:
    from app.tasks.event_pipeline import process_event

    _safe_delay("process_event", process_event, str(event_id))


def dispatch_state_replay(event_id: UUID, before_state_version: int) -> None:
    from app.tasks.replay import state_replay

    _safe_delay("state_replay", state_replay, str(event_id), before_state_version)


def dispatch_state_rebuild(entity_id: UUID, job_id: UUID) -> None:
    from app.tasks.rebuild import state_rebuild

    _safe_delay("state_rebuild", state_rebuild, str(entity_id), str(job_id))


def dispatch_wiki_sync(task_id: UUID, job_id: UUID) -> None:
    from app.tasks.wiki_sync import sync_wiki

    _safe_delay("sync_wiki", sync_wiki, str(task_id), str(job_id))
