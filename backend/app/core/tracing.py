from __future__ import annotations

import structlog
from fastapi import FastAPI

from app.core.config import Settings

logger = structlog.get_logger(__name__)


def configure_tracing(app: FastAPI, settings: Settings) -> None:
    """按 OTEL_EXPORTER_OTLP_ENDPOINT 接入 OTLP HTTP 链路导出；失败不阻断启动。"""
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name}),
            sampler=TraceIdRatioBased(settings.otel_traces_sampler_ratio),
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    except Exception as exc:  # noqa: BLE001 - 观测初始化失败不影响服务启动
        logger.warning(
            "otel_tracing_init_failed",
            error_type=type(exc).__name__,
            endpoint=endpoint,
        )
        return
    logger.info("otel_tracing_enabled", endpoint=endpoint, service=settings.otel_service_name)
