from __future__ import annotations

import math

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import redact_secrets, sanitize_url
from app.services.remote_checks import (
    RemoteCheckError,
    RemoteModelChecker,
    normalize_rerank_scores,
    validate_embedding_response,
    validate_remote_base_url,
    validate_rerank_response,
)


def embedding_payload(model: str = "bge-m3") -> dict:
    return {
        "model": model,
        "data": [{"embedding": [3.0] + [0.0] * 1023}],
    }


def remote_settings() -> Settings:
    return Settings(
        _env_file=None,
        embedding_provider="remote",
        embedding_model_name="bge-m3",
        embedding_api_base="https://models.example.test/v1",
        embedding_api_key=SecretStr("top-secret-token"),
        reranker_enabled=False,
    )


def test_embedding_dimension_and_l2_normalization_checks() -> None:
    result = validate_embedding_response(embedding_payload(), expected_model="bge-m3")

    assert result.dimension == 1024
    assert result.source_norm == 3.0
    assert math.isclose(result.normalized_norm, 1.0)
    with pytest.raises(RemoteCheckError) as error:
        validate_embedding_response({"data": [{"embedding": [1.0, 2.0]}]}, expected_model="bge-m3")
    assert error.value.code == "INVALID_DIMENSION"
    with pytest.raises(RemoteCheckError) as error:
        validate_embedding_response(embedding_payload(), expected_model="other")
    assert error.value.code == "MODEL_MISMATCH"


def test_rerank_probability_logit_and_document_order() -> None:
    probability = validate_rerank_response(
        {"results": [{"index": 1, "relevance_score": 0.2}, {"index": 0, "score": 0.8}]},
        expected_model="",
    )
    logit = normalize_rerank_scores([0.0, 2.0])
    extreme = normalize_rerank_scores([-1000.0, 1000.0])

    assert probability.scores == (0.8, 0.2)
    assert probability.score_mode == "probability"
    assert logit.score_mode == "logit"
    assert logit.scores[0] == 0.5
    assert 0 < logit.scores[1] < 1
    assert extreme.scores == (0.0, 1.0)


def test_remote_url_rejects_embedded_credentials_and_requires_tls() -> None:
    with pytest.raises(RemoteCheckError) as error:
        validate_remote_base_url("https://user:password@example.test/v1")
    assert error.value.code == "INVALID_URL"
    with pytest.raises(RemoteCheckError) as error:
        validate_remote_base_url("http://example.test/v1", production=True)
    assert error.value.code == "TLS_REQUIRED"


def test_remote_checker_retries_rate_limit_without_exposing_secret() -> None:
    responses = [
        httpx.Response(429, json={"error": "busy"}),
        httpx.Response(200, json=embedding_payload()),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    sleeps: list[float] = []
    checker = RemoteModelChecker(
        remote_settings(), transport=httpx.MockTransport(handler), sleep=sleeps.append
    )

    result = checker.check_embedding()

    assert result is not None and result.dimension == 1024
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer top-secret-token"
    assert sleeps == [0.25]
    safe_error = checker.safe_error(RuntimeError("Bearer top-secret-token"))
    assert "top-secret-token" not in safe_error
    assert "[REDACTED]" in safe_error


def test_remote_checker_retries_timeout_then_returns_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    checker = RemoteModelChecker(
        remote_settings(), transport=httpx.MockTransport(handler), sleep=lambda _: None
    )

    with pytest.raises(RemoteCheckError) as error:
        checker.check_embedding()

    assert error.value.code == "NETWORK_ERROR"
    assert "top-secret-token" not in str(error.value)
    assert "models.example.test/v1/embeddings" in str(error.value)


def test_log_sanitization_removes_url_credentials_and_secret_fields() -> None:
    url = "https://user:password@example.test:8443/v1?api_key=secret#fragment"
    value = redact_secrets(
        {
            "api_base": url,
            "embedding_api_base": url,
            "api_key": "secret",
            "nested": {"Authorization": "Bearer token"},
        }
    )

    assert sanitize_url(url) == "https://example.test:8443/v1"
    assert value == {
        "api_base": "https://example.test:8443/v1",
        "embedding_api_base": "https://example.test:8443/v1",
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]"},
    }
