from __future__ import annotations

from datetime import UTC, datetime

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db import get_session_factory
from app.models import OutboxEvent, OutboxStatus

logger = structlog.get_logger(__name__)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_REQUEST_ERRORS = Counter(
    "http_request_errors_total",
    "HTTP 错误响应总数",
    ["route", "status"],
)
RATE_LIMIT_REJECTIONS = Counter(
    "rate_limit_rejections_total",
    "限流拒绝次数",
    ["scope"],
)
EVENTS_PROCESSED = Counter(
    "events_processed_total",
    "事件管道处理结果",
    ["status"],
)
OUTBOX_DISPATCHED = Counter(
    "outbox_dispatched_total",
    "Outbox 派发结果",
    ["result"],
)
OUTBOX_PENDING = Gauge(
    "outbox_pending",
    "等待派发的 Outbox 记录数",
)
OUTBOX_DEAD_LETTER = Gauge(
    "outbox_dead_letter",
    "Outbox 死信数量",
)
OUTBOX_OLDEST_PENDING_SECONDS = Gauge(
    "outbox_oldest_pending_seconds",
    "最老待派发 Outbox 记录的等待秒数",
)


def record_request(method: str, route: str, status: int, latency_seconds: float) -> None:
    status_text = str(status)
    HTTP_REQUESTS.labels(method=method, route=route, status=status_text).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(latency_seconds)
    if status >= 500:
        HTTP_REQUEST_ERRORS.labels(route=route, status=status_text).inc()


def record_rate_limited(scope: str) -> None:
    RATE_LIMIT_REJECTIONS.labels(scope=scope).inc()


def record_event_processed(status: str) -> None:
    EVENTS_PROCESSED.labels(status=status).inc()


def record_outbox_dispatched(result: str) -> None:
    OUTBOX_DISPATCHED.labels(result=result).inc()


def refresh_outbox_gauges(settings: Settings) -> None:
    """刷新 Outbox 积压指标；数据库不可用时保留上一次值。"""
    if not settings.database_url:
        return
    session = None
    try:
        session = get_session_factory(settings.database_url)()
        pending = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
        )
        dead_letter = session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.DEAD_LETTER)
        )
        oldest = session.scalar(
            select(func.min(OutboxEvent.created_at)).where(
                OutboxEvent.status == OutboxStatus.PENDING
            )
        )
    except Exception as exc:  # noqa: BLE001 - 指标刷新不能影响抓取
        logger.warning("outbox_metrics_refresh_failed", error_type=type(exc).__name__)
        return
    finally:
        if session is not None:
            session.close()

    OUTBOX_PENDING.set(int(pending or 0))
    OUTBOX_DEAD_LETTER.set(int(dead_letter or 0))
    if oldest is None:
        OUTBOX_OLDEST_PENDING_SECONDS.set(0)
    else:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        OUTBOX_OLDEST_PENDING_SECONDS.set(max(0.0, (datetime.now(UTC) - oldest).total_seconds()))


def render_metrics(settings: Settings) -> tuple[bytes, str]:
    refresh_outbox_gauges(settings)
    return generate_latest(), CONTENT_TYPE_LATEST
