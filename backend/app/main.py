from __future__ import annotations

from fastapi import FastAPI

from app.api.metrics import router as metrics_router
from app.api.v1.admin import router as admin_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.entities import router as entities_router
from app.api.v1.health import router as health_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.memory import router as memory_router
from app.api.v1.outbox import router as outbox_router
from app.api.v1.wiki import router as wiki_router
from app.api.v1.working import router as working_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import register_middleware
from app.core.tracing import configure_tracing

API_V1_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Layered Memory System API",
        version="0.1.0",
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings or get_settings()
    register_middleware(app)
    configure_tracing(app, app.state.settings)
    register_exception_handlers(app)
    app.include_router(metrics_router)
    app.include_router(health_router, prefix=API_V1_PREFIX)
    app.include_router(memory_router, prefix=API_V1_PREFIX)
    app.include_router(working_router, prefix=API_V1_PREFIX)
    app.include_router(entities_router, prefix=API_V1_PREFIX)
    app.include_router(wiki_router, prefix=API_V1_PREFIX)
    app.include_router(jobs_router, prefix=API_V1_PREFIX)
    app.include_router(outbox_router, prefix=API_V1_PREFIX)
    app.include_router(dashboard_router, prefix=API_V1_PREFIX)
    app.include_router(admin_router, prefix=API_V1_PREFIX)
    return app


app = create_app()
