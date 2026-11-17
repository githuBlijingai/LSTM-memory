from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app


def test_production_rejects_dev_auth() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", dev_auth_enabled=True)


def test_missing_configuration_covers_remote_models_and_auth() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        embedding_provider="remote",
        reranker_enabled=False,
    )

    missing = settings.missing_configuration()

    assert "DATABASE_URL" in missing
    assert "EMBEDDING_API_BASE" in missing
    assert "EMBEDDING_API_KEY" in missing
    assert "OIDC_JWKS_URL" in missing


def test_live_health_and_structured_error_include_same_request_id() -> None:
    app = create_app(Settings(_env_file=None))

    @app.get("/test-error")
    def test_error() -> None:
        raise AppError("CONFLICT", "测试冲突", 409, {"field": "value"})

    client = TestClient(app)
    live = client.get("/api/v1/health/live")
    request_id = "9b80a2c2-16f4-4f0f-a85c-f21b6b0677ce"
    error = client.get("/test-error", headers={"X-Request-ID": request_id})

    assert live.status_code == 200
    assert live.json()["data"] == {"status": "ok", "ready": None}
    assert UUID(live.json()["meta"]["request_id"])
    assert error.status_code == 409
    assert error.headers["X-Request-ID"] == request_id
    assert error.json() == {
        "error": {
            "code": "CONFLICT",
            "message": "测试冲突",
            "details": {"field": "value"},
        },
        "meta": {"request_id": request_id, "next_cursor": None},
    }


def test_ready_health_returns_503_without_external_dependencies() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["data"]["ready"] is False
    assert response.json()["data"]["checks"] == {
        "configuration": False,
        "database": False,
        "redis": False,
        "models": False,
    }
    assert response.json()["data"]["model_checks"] == {
        "event_model": False,
        "embedding": False,
        "reranker": False,
    }
