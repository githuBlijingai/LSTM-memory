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


class EmbeddingProvider(Protocol):
    model_version: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与输入等长、L2 归一化的基础向量。"""


def normalize_vectors(vectors: list[Any], *, dimension: int) -> list[list[float]]:
    if dimension <= 0:
        raise ValueError("dimension must be positive")
    normalized: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, (list, tuple)):
            raise AppError("EMBEDDING_INVALID", "Embedding 向量类型无效", 502)
        numbers = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AppError("EMBEDDING_INVALID", "Embedding 包含非数值", 502)
            number = float(value)
            if not math.isfinite(number):
                raise AppError("EMBEDDING_INVALID", "Embedding 包含 NaN 或无穷值", 502)
            numbers.append(number)
        if len(numbers) != dimension:
            raise AppError(
                "EMBEDDING_INVALID",
                f"Embedding 维度必须为 {dimension}，实际为 {len(numbers)}",
                502,
            )
        norm = math.sqrt(math.fsum(value * value for value in numbers))
        if not math.isfinite(norm) or norm == 0:
            raise AppError("EMBEDDING_INVALID", "Embedding 向量不能为零向量", 502)
        normalized.append([value / norm for value in numbers])
    return normalized


class RemoteEmbeddingProvider:
    """OpenAI 兼容 /embeddings 远程 Provider。"""

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
        self.model_version = settings.embedding_model_version or ""
        self._api_base = settings.embedding_api_base or ""
        self._model_name = settings.embedding_model_name or ""
        self._api_key = (
            settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else ""
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if (
            not self._api_base
            or not self._model_name
            or not self._api_key
            or not self.model_version
        ):
            raise AppError("MODEL_UNAVAILABLE", "远程 Embedding 配置不完整", 503)
        try:
            base = validate_remote_base_url(self._api_base, production=self.settings.is_production)
        except RemoteCheckError as exc:
            raise AppError(
                "MODEL_UNAVAILABLE", f"Embedding 服务地址无效：{exc.safe_message}", 503
            ) from exc
        payload = self._post(
            f"{base}/embeddings",
            {"model": self._model_name, "input": texts, "encoding_format": "float"},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise AppError("EMBEDDING_INVALID", "远程 Embedding 返回数量与输入不一致", 502)
        if isinstance(payload, dict) and payload.get("model") not in {None, self._model_name}:
            raise AppError("EMBEDDING_INVALID", "远程 Embedding 模型名称不匹配", 502)
        if any(isinstance(item, dict) and item.get("index") is not None for item in data):
            data = sorted(data, key=lambda item: item.get("index", 0))
        vectors = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise AppError("EMBEDDING_INVALID", "远程 Embedding 响应结构无效", 502)
            vectors.append(item["embedding"])
        return normalize_vectors(vectors, dimension=self.settings.base_embedding_dim)

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
                        "MODEL_NETWORK_ERROR",
                        f"远程 Embedding 服务连接失败：{safe_url}",
                        503,
                    ) from exc
                if response.status_code in {401, 403}:
                    raise AppError("MODEL_AUTH_FAILED", "远程 Embedding 服务鉴权失败", 502)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < MAX_REMOTE_ATTEMPTS - 1:
                        self.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                        continue
                    raise AppError("MODEL_RATE_LIMITED", "远程 Embedding 服务持续不可用", 503)
                if response.is_error:
                    raise AppError(
                        "MODEL_ERROR",
                        f"远程 Embedding 服务返回 HTTP {response.status_code}：{safe_url}",
                        502,
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise AppError(
                        "EMBEDDING_INVALID", "远程 Embedding 响应不是合法 JSON", 502
                    ) from exc
        raise AppError("MODEL_NETWORK_ERROR", "远程 Embedding 调用失败", 503)

    def safe_error(self, error: Exception) -> str:
        return redact_text(str(error), (self._api_key,))


class LocalBGEProvider:
    """本地 BGE Provider；延迟导入 transformers/torch。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_version = settings.embedding_model_version or ""
        if not settings.embedding_model_name or not self.model_version:
            raise AppError("MODEL_UNAVAILABLE", "本地 Embedding 配置不完整", 503)
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise AppError("MODEL_UNAVAILABLE", "缺少 torch/transformers 依赖", 503) from exc
        self._torch = torch
        self._device = settings.model_device or "cpu"
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(settings.embedding_model_name)
            self._model = AutoModel.from_pretrained(settings.embedding_model_name)
            self._model.to(self._device)
            self._model.eval()
        except (OSError, ValueError) as exc:
            raise AppError("MODEL_UNAVAILABLE", "本地 Embedding 模型加载失败", 503) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        torch = self._torch
        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded = {key: value.to(self._device) for key, value in encoded.items()}
        with torch.no_grad():
            output = self._model(**encoded)
            # BGE 使用 CLS 向量作为句向量。
            vectors = output.last_hidden_state[:, 0, :]
            vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        return normalize_vectors(
            vectors.detach().cpu().tolist(), dimension=self.settings.base_embedding_dim
        )
