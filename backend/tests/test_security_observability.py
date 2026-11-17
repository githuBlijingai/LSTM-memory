from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.rate_limit import rate_limit
from app.core.security import require_viewer
from app.main import create_app


def _client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, **overrides)))


def test_security_headers_are_added_to_success_and_error_responses() -> None:
    client = _client()

    success = client.get("/api/v1/health/live")
    missing = client.get("/api/v1/does-not-exist")

    for response in (success, missing):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Strict-Transport-Security" not in response.headers


def test_hsts_header_only_when_configured() -> None:
    client = _client(hsts_max_age_seconds=31536000)

    response = client.get("/api/v1/health/live")

    assert response.headers["Strict-Transport-Security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_request_body_over_limit_returns_413_envelope() -> None:
    app = create_app(Settings(_env_file=None, max_request_body_bytes=1024))

    @app.post("/test-body")
    def test_body() -> dict:
        return {"ok": True}

    response = TestClient(app).post("/test-body", content=b"x" * 2048)

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert body["error"]["details"]["max_bytes"] == 1024
    assert body["meta"]["request_id"]
    assert response.headers["X-Request-ID"] == body["meta"]["request_id"]


def test_rate_limit_rejects_second_request_with_retry_after() -> None:
    settings = Settings(
        _env_file=None,
        dev_auth_enabled=True,
        rate_limit_search_per_minute=1,
    )
    app = create_app(settings)
    router = APIRouter()
    dependency = rate_limit(
        "test", "rate_limit_search_per_minute", 60, require_viewer
    )

    @router.get("/rate-test", dependencies=[Depends(dependency)])
    def rate_test() -> dict:
        return {"ok": True}

    app.include_router(router)
    client = TestClient(app)
    headers = {"Authorization": "dev alice", "X-Dev-Roles": "viewer"}

    first = client.get("/rate-test", headers=headers)
    second = client.get("/rate-test", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert second.headers["Retry-After"]
    assert response_scope_counter() >= 1


def response_scope_counter() -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "rate_limit_rejections_total", {"scope": "test"}
    ) or 0.0


def test_rate_limit_can_be_disabled() -> None:
    settings = Settings(
        _env_file=None,
        dev_auth_enabled=True,
        rate_limit_enabled=False,
        rate_limit_search_per_minute=1,
    )
    app = create_app(settings)
    router = APIRouter()
    dependency = rate_limit(
        "test-disabled", "rate_limit_search_per_minute", 60, require_viewer
    )

    @router.get("/rate-test", dependencies=[Depends(dependency)])
    def rate_test() -> dict:
        return {"ok": True}

    app.include_router(router)
    client = TestClient(app)
    headers = {"Authorization": "dev alice", "X-Dev-Roles": "viewer"}

    assert client.get("/rate-test", headers=headers).status_code == 200
    assert client.get("/rate-test", headers=headers).status_code == 200


def test_metrics_endpoint_exposes_prometheus_text() -> None:
    client = _client()
    client.get("/api/v1/health/live")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text
    assert "http_request_duration_seconds" in response.text


def test_metrics_token_is_enforced_when_configured() -> None:
    client = _client(metrics_token="secret-token")

    unauthorized = client.get("/metrics")
    authorized = client.get("/metrics", headers={"Authorization": "Bearer secret-token"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_metrics_can_be_disabled() -> None:
    client = _client(metrics_enabled=False)

    assert client.get("/metrics").status_code == 404


def test_ready_model_check_is_cached(monkeypatch) -> None:
    from app.services.readiness import ModelCheckResult, ModelReadinessChecker

    calls = {"count": 0}

    def fake_run(self) -> ModelCheckResult:
        calls["count"] += 1
        return ModelCheckResult(
            ready=False,
            checks={"event_model": False, "embedding": False, "reranker": False},
        )

    monkeypatch.setattr(ModelReadinessChecker, "_run", fake_run)
    client = _client()

    first = client.get("/api/v1/health/ready")
    second = client.get("/api/v1/health/ready")

    assert first.status_code == 503
    assert second.status_code == 503
    assert calls["count"] == 1
    assert second.json()["data"]["checks"]["models"] is False


def test_ready_reports_model_checks_from_local_package(monkeypatch, tmp_path) -> None:
    import json

    import app.services.readiness as readiness

    model_dir = tmp_path / "event-model"
    model_dir.mkdir()
    (model_dir / "manifest.json").write_text(
        json.dumps({"model_version": "v1", "weights_file": "weights.safetensors"}),
        encoding="utf-8",
    )
    (model_dir / "weights.safetensors").write_bytes(b"weights")

    monkeypatch.setattr(readiness, "_provider_ready", lambda *args, **kwargs: True)
    client = _client(
        event_model_uri=str(model_dir),
        event_model_version="v1",
        embedding_provider="remote",
        embedding_model_name="bge",
        reranker_enabled=False,
    )

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["models"] is True
    assert response.json()["data"]["model_checks"] == {
        "event_model": True,
        "embedding": True,
        "reranker": True,
    }


def test_remote_reachability_fails_fast_for_closed_port() -> None:
    from app.services.readiness import _remote_reachable

    assert _remote_reachable("http://127.0.0.1:1", timeout=0.5) is False


