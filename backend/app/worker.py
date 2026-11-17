from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery(
    "memory_system",
    broker=settings.redis_url,
    backend=None,
    include=[
        "app.tasks.outbox",
        "app.tasks.event_pipeline",
        "app.tasks.replay",
        "app.tasks.rebuild",
        "app.tasks.wiki_sync",
        "app.tasks.maintenance",
    ],
)
celery_app.conf.update(
    task_acks_late=True,
    task_acks_on_failure_or_timeout=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_store_errors_even_if_ignored=False,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "outbox-dispatch": {
            "task": "app.tasks.outbox.dispatch_outbox",
            "schedule": 1.0,
        },
        "daily-maintenance": {
            "task": "app.tasks.maintenance.run_daily_maintenance",
            "schedule": crontab(
                hour=settings.maintenance_utc_hour,
                minute=settings.maintenance_utc_minute,
            ),
        },
    },
)
