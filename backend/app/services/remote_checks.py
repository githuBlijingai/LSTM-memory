from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import redact_text, sanitize_url

EMBEDDING_DIMENSION = 1024


class RemoteCheckError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class EmbeddingCheckResult:
    dimension: int
    source_norm: float
    normalized_norm: float
    model: str


@dataclass(frozen=True)
class RerankCheckResult:
    scores: tuple[float, float]
    score_mode: Literal["probability", "logit"]
    model: str


def validate_remote_base_url(base_url: str, *, production: bool = False) -> str:
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError as exc:
        raise RemoteCheckError("INVALID_URL", "远程模型 Base URL 无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RemoteCheckError("INVALID_URL", "远程模型 Base URL 必须是有效的 HTTP(S) 地址")
    if production and parsed.scheme != "https":
        raise RemoteCheckError("TLS_REQUIRED", "生产环境远程模型服务必须使用 TLS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RemoteCheckError(
            "INVALID_URL", "Base URL 不得包含用户信息、查询参数或片段；密钥只能使用环境变量"
        )
    return base_url.rstrip("/")


def validate_embedding_response(payload: Any, *, expected_model: str) -> EmbeddingCheckResult:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("data"), list)
        or not payload["data"]
    ):
        raise RemoteCheckError("INVALID_RESPONSE", "Embedding 响应缺少 data 数组")
    if payload.get("model") not in {None, expected_model}:
        raise RemoteCheckError("MODEL_MISMATCH", "远程服务返回的 Embedding 模型名称不匹配")
    first = payload["data"][0]
    if not isinstance(first, dict) or not isinstance(first.get("embedding"), list):
        raise RemoteCheckError("INVALID_RESPONSE", "Embedding 响应缺少 embedding 数组")
    values = _finite_numbers(first["embedding"], "Embedding")
    if len(values) != EMBEDDING_DIMENSION:
        raise RemoteCheckError(
            "INVALID_DIMENSION", f"Embedding 维度必须为 {EMBEDDING_DIMENSION}，实际为 {len(values)}"
        )
    norm = math.sqrt(math.fsum(value * value for value in values))
    if not math.isfinite(norm) or norm == 0:
        raise RemoteCheckError("INVALID_VECTOR", "Embedding 向量不能为零向量")
    normalized = [value / norm for value in values]
    normalized_norm = math.sqrt(math.fsum(value * value for value in normalized))
    if not math.isclose(normalized_norm, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise RemoteCheckError("INVALID_VECTOR", "Embedding L2 归一化检查失败")
    return EmbeddingCheckResult(len(values), norm, normalized_norm, expected_model)


def normalize_rerank_scores(
    scores: list[Any], *, mode: Literal["auto", "probability", "logit"] = "auto"
) -> RerankCheckResult:
    values = _finite_numbers(scores, "Rerank")
    if len(values) != 2:
        raise RemoteCheckError("INVALID_RESPONSE", "Rerank 检查必须返回两个文档分数")
    if mode == "probability" or (mode == "auto" and all(0 <= value <= 1 for value in values)):
        if not all(0 <= value <= 1 for value in values):
            raise RemoteCheckError("INVALID_SCORE", "Rerank 概率分数必须位于 [0,1]")
        return RerankCheckResult((values[0], values[1]), "probability", "")
    normalized = tuple(
        1 / (1 + math.exp(-value)) if value >= 0 else math.exp(value) / (1 + math.exp(value))
        for value in values
    )
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in normalized):
        raise RemoteCheckError("INVALID_SCORE", "Rerank Logit 归一化失败")
    return RerankCheckResult(normalized, "logit", "")


def validate_rerank_response(payload: Any, *, expected_model: str) -> RerankCheckResult:
    if isinstance(payload, dict) and payload.get("model") not in {None, expected_model}:
        raise RemoteCheckError("MODEL_MISMATCH", "远程服务返回的 Rerank 模型名称不匹配")
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        results = payload["results"]
        if len(results) != 2 or any(not isinstance(item, dict) for item in results):
            raise RemoteCheckError("INVALID_RESPONSE", "Rerank results 结构无效")
        try:
            ordered = sorted(results, key=lambda item: item.get("index", 0))
            scores = [item.get("relevance_score", item.get("score")) for item in ordered]
        except (TypeError, AttributeError) as exc:
            raise RemoteCheckError("INVALID_RESPONSE", "Rerank results 结构无效") from exc
    elif isinstance(payload, dict) and isinstance(payload.get("scores"), list):
        scores = payload["scores"]
    elif isinstance(payload, list):
        scores = payload
    else:
        raise RemoteCheckError("INVALID_RESPONSE", "Rerank 响应必须包含 results 或 scores")
    result = normalize_rerank_scores(scores)
    return RerankCheckResult(result.scores, result.score_mode, expected_model)


def _finite_numbers(values: list[Any], label: str) -> list[float]:
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RemoteCheckError("INVALID_RESPONSE", f"{label} 数值类型无效")
        number = float(value)
        if not math.isfinite(number):
            raise RemoteCheckError("INVALID_RESPONSE", f"{label} 包含 NaN 或无穷值")
        numbers.append(number)
    return numbers


class RemoteModelChecker:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.sleep = sleep

    def check_embedding(self) -> EmbeddingCheckResult | None:
        if self.settings.embedding_provider != "remote":
            return None
        base = validate_remote_base_url(
            self.settings.embedding_api_base or "", production=self.settings.is_production
        )
        model = self.settings.embedding_model_name or ""
        key = self._secret(self.settings.embedding_api_key)
        response = self._post(
            f"{base}/embeddings",
            json_body={"model": model, "input": ["memory system readiness probe"]},
            api_key=key,
        )
        return validate_embedding_response(response, expected_model=model)

    def check_reranker(self) -> RerankCheckResult | None:
        if not self.settings.reranker_enabled or self.settings.reranker_provider != "remote":
            return None
        base = validate_remote_base_url(
            self.settings.reranker_api_base or "", production=self.settings.is_production
        )
        model = self.settings.reranker_model_name or ""
        key = self._secret(self.settings.reranker_api_key)
        response = self._post(
            f"{base}/rerank",
            json_body={
                "model": model,
                "query": "memory readiness",
                "documents": ["first probe document", "second probe document"],
            },
            api_key=key,
        )
        return validate_rerank_response(response, expected_model=model)

    def check_all(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        embedding = self.check_embedding()
        if embedding:
            result["embedding"] = {
                "provider": "remote",
                "model": embedding.model,
                "dimension": embedding.dimension,
                "normalized_norm": round(embedding.normalized_norm, 6),
                "api_base": sanitize_url(self.settings.embedding_api_base),
            }
        reranker = self.check_reranker()
        if reranker:
            result["reranker"] = {
                "provider": "remote",
                "model": reranker.model,
                "score_mode": reranker.score_mode,
                "api_base": sanitize_url(self.settings.reranker_api_base),
            }
        return result

    def _post(self, url: str, *, json_body: dict, api_key: str) -> Any:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        safe_url = sanitize_url(url)
        with httpx.Client(
            timeout=self.settings.model_http_timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            for attempt in range(3):
                try:
                    response = client.post(url, json=json_body, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < 2:
                        self.sleep(0.25 * (2**attempt))
                        continue
                    raise RemoteCheckError(
                        "NETWORK_ERROR", f"远程模型服务连接失败：{safe_url}；请检查地址、TLS 和超时"
                    ) from exc
                if response.status_code in {401, 403}:
                    raise RemoteCheckError(
                        "AUTH_FAILED", f"远程模型服务鉴权失败：{safe_url}；请检查对应 API Key"
                    )
                if response.status_code == 429:
                    if attempt < 2:
                        self.sleep(0.25 * (2**attempt))
                        continue
                    raise RemoteCheckError("RATE_LIMITED", "远程模型服务持续限流，请稍后重试")
                if response.is_error:
                    raise RemoteCheckError(
                        "HTTP_ERROR", f"远程模型服务返回 HTTP {response.status_code}：{safe_url}"
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise RemoteCheckError(
                        "INVALID_RESPONSE", "远程模型服务未返回合法 JSON"
                    ) from exc
        raise RemoteCheckError("NETWORK_ERROR", "远程模型服务检查失败")

    @staticmethod
    def _secret(value: SecretStr | None) -> str:
        return value.get_secret_value() if value else ""

    def safe_error(self, error: Exception) -> str:
        secrets = tuple(
            secret
            for secret in (
                self._secret(self.settings.embedding_api_key),
                self._secret(self.settings.reranker_api_key),
            )
            if secret
        )
        return redact_text(str(error), secrets)
