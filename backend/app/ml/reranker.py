from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import redact_text, sanitize_url
from app.services.remote_checks import RemoteCheckError, validate_remote_base_url

MAX_REMOTE_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.25


class Reranker(Protocol):
    model_version: str

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长、位于 [0,1] 的相关性分数。"""


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def normalize_rerank_scores(scores: list[Any], *, expected_count: int) -> list[float]:
    if len(scores) != expected_count:
        raise AppError("RERANK_INVALID", "Rerank 分数数量与文档数量不一致", 502)
    numbers: list[float] = []
    for value in scores:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AppError("RERANK_INVALID", "Rerank 分数类型无效", 502)
        number = float(value)
        if not math.isfinite(number):
            raise AppError("RERANK_INVALID", "Rerank 分数包含 NaN 或无穷值", 502)
        numbers.append(number)
    if all(0 <= value <= 1 for value in numbers):
        return numbers
    # 兼容返回 logit 的远程服务：仅在越界时做一次 sigmoid 归一化。
    normalized = [_sigmoid(value) for value in numbers]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in normalized):
        raise AppError("RERANK_INVALID", "Rerank 分数归一化失败", 502)
    return normalized


class RemoteReranker:
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
        self.model_version = settings.reranker_model_version or ""
        self._api_base = settings.reranker_api_base or ""
        self._model_name = settings.reranker_model_name or ""
        self._api_key = (
            settings.reranker_api_key.get_secret_value() if settings.reranker_api_key else ""
        )

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if (
            not self._api_base
            or not self._model_name
            or not self._api_key
            or not self.model_version
        ):
            raise AppError("MODEL_UNAVAILABLE", "远程 Reranker 配置不完整", 503)
        try:
            base = validate_remote_base_url(self._api_base, production=self.settings.is_production)
        except RemoteCheckError as exc:
            raise AppError(
                "MODEL_UNAVAILABLE", f"Reranker 服务地址无效：{exc.safe_message}", 503
            ) from exc
        payload = self._post(
            f"{base}/rerank",
            {"model": self._model_name, "query": query, "documents": documents},
        )
        scores = self._extract_scores(payload)
        if isinstance(payload, dict) and payload.get("model") not in {None, self._model_name}:
            raise AppError("RERANK_INVALID", "远程 Rerank 模型名称不匹配", 502)
        return normalize_rerank_scores(scores, expected_count=len(documents))

    @staticmethod
    def _extract_scores(payload: Any) -> list[Any]:
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            results = payload["results"]
            if any(not isinstance(item, dict) for item in results):
                raise AppError("RERANK_INVALID", "Rerank results 结构无效", 502)
            ordered = sorted(results, key=lambda item: item.get("index", 0))
            return [item.get("relevance_score", item.get("score")) for item in ordered]
        if isinstance(payload, dict) and isinstance(payload.get("scores"), list):
            return list(payload["scores"])
        if isinstance(payload, list):
            return list(payload)
        raise AppError("RERANK_INVALID", "Rerank 响应必须包含 results 或 scores", 502)

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        safe_url = sanitize_url(url)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        with httpx.Client(
            timeout=self.settings.model_http_timeout_seconds,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            for attempt in range(MAX_REMOTE_ATTEMPTS):
                try:
                    response = client.post(url, json=body, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < MAX_REMOTE_ATTEMPTS - 1:
                        self.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                        continue
                    raise AppError(
                        "MODEL_NETWORK_ERROR", f"远程 Rerank 服务连接失败：{safe_url}", 503
                    ) from exc
                if response.status_code in {401, 403}:
                    raise AppError("MODEL_AUTH_FAILED", "远程 Rerank 服务鉴权失败", 502)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < MAX_REMOTE_ATTEMPTS - 1:
                        self.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                        continue
                    raise AppError("MODEL_RATE_LIMITED", "远程 Rerank 服务持续不可用", 503)
                if response.is_error:
                    raise AppError(
                        "MODEL_ERROR",
                        f"远程 Rerank 服务返回 HTTP {response.status_code}：{safe_url}",
                        502,
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise AppError("RERANK_INVALID", "远程 Rerank 响应不是合法 JSON", 502) from exc
        raise AppError("MODEL_NETWORK_ERROR", "远程 Rerank 调用失败", 503)

    def safe_error(self, error: Exception) -> str:
        return redact_text(str(error), (self._api_key,))


class LocalReranker:
    """本地交叉编码器；延迟导入 transformers/torch。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_version = settings.reranker_model_version or ""
        if not settings.reranker_model_name or not self.model_version:
            raise AppError("MODEL_UNAVAILABLE", "本地 Reranker 配置不完整", 503)
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise AppError("MODEL_UNAVAILABLE", "缺少 torch/transformers 依赖", 503) from exc
        self._torch = torch
        self._device = settings.model_device or "cpu"
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(settings.reranker_model_name)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                settings.reranker_model_name
            )
            self._model.to(self._device)
            self._model.eval()
        except (OSError, ValueError) as exc:
            raise AppError("MODEL_UNAVAILABLE", "本地 Reranker 模型加载失败", 503) from exc

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        torch = self._torch
        scores: list[float] = []
        for document in documents:
            encoded = self._tokenizer(
                query,
                document,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with torch.no_grad():
                logits = self._model(**encoded).logits
            if logits.shape[-1] == 1:
                score = torch.sigmoid(logits[0, 0]).item()
            else:
                score = torch.softmax(logits[0], dim=-1)[-1].item()
            scores.append(score)
        return normalize_rerank_scores(scores, expected_count=len(documents))
