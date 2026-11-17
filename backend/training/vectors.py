"""基础向量解析：优先使用数据集中预计算的向量，否则按配置调用 Embedding Provider。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.ml.embedding import EmbeddingProvider, LocalBGEProvider, RemoteEmbeddingProvider
from training.dataset import TrainingDataset

EMBED_BATCH_SIZE = 32


class VectorError(RuntimeError):
    """向量缺失或配置不可用。"""


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """按 Settings 构造本地/远程 Embedding Provider，与运行期选择保持一致。"""
    if settings.embedding_provider == "local":
        return LocalBGEProvider(settings)
    if settings.embedding_provider == "remote":
        return RemoteEmbeddingProvider(settings)
    raise VectorError("未配置 EMBEDDING_PROVIDER，无法在训练时计算基础向量")


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TextVectorizer:
    """带 JSONL 缓存的基础向量解析器，避免重复调用 Embedding 服务。"""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        self.provider = provider
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._cache: dict[str, list[float]] = {}
        if self.cache_path is not None and self.cache_path.is_file():
            self._load_cache()

    def _load_cache(self) -> None:
        assert self.cache_path is not None
        for line in self.cache_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                key = item["key"]
                vector = item["vector"]
            except (ValueError, KeyError) as exc:
                raise VectorError(f"向量缓存格式无效：{self.cache_path}") from exc
            if isinstance(key, str) and isinstance(vector, list) and vector:
                self._cache[key] = [float(value) for value in vector]

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as handle:
            for key, vector in sorted(self._cache.items()):
                handle.write(json.dumps({"key": key, "vector": vector}) + "\n")

    def missing(self, texts: Iterable[str]) -> list[str]:
        return [text for text in dict.fromkeys(texts) if _text_key(text) not in self._cache]

    def prefetch(self, texts: Iterable[str]) -> None:
        pending = self.missing(texts)
        if not pending:
            return
        if self.provider is None:
            raise VectorError("数据集缺少 base_vector，且未配置 Embedding Provider")
        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start : start + EMBED_BATCH_SIZE]
            vectors = self.provider.embed(batch)
            if len(vectors) != len(batch):
                raise VectorError("Embedding 返回数量与输入不一致")
            for text, vector in zip(batch, vectors, strict=True):
                self._cache[_text_key(text)] = [float(value) for value in vector]
        self._save_cache()

    def get(self, text: str) -> list[float]:
        key = _text_key(text)
        if key not in self._cache:
            self.prefetch([text])
        return self._cache[key]

    def update(self, text: str, vector: Iterable[float]) -> None:
        self._cache[_text_key(text)] = [float(value) for value in vector]


def _dimension(vectors: Iterable[list[float]]) -> int:
    dimension = 0
    for vector in vectors:
        if not vector:
            raise VectorError("基础向量不能为空")
        if dimension == 0:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise VectorError(f"基础向量维度不一致：期望 {dimension}，实际 {len(vector)}")
    if dimension == 0:
        raise VectorError("数据集没有任何基础向量")
    return dimension


def resolve_vectors(
    dataset: TrainingDataset,
    vectorizer: TextVectorizer,
) -> tuple[dict[UUID, list[float]], dict[UUID, list[float]]]:
    """解析事件与查询的基础向量，缺失时调用 Provider 并写入缓存。"""
    events = dataset.usable_events()
    if not events and not dataset.queries:
        return {}, {}
    pending = [
        record.text for record in [*events.values(), *dataset.queries] if record.base_vector is None
    ]
    if pending:
        vectorizer.prefetch(pending)

    event_vectors = {
        event_id: (
            list(event.base_vector) if event.base_vector is not None else vectorizer.get(event.text)
        )
        for event_id, event in events.items()
    }
    query_vectors = {
        query.query_id: (
            list(query.base_vector) if query.base_vector is not None else vectorizer.get(query.text)
        )
        for query in dataset.queries
    }
    _dimension([*event_vectors.values(), *query_vectors.values()])
    return event_vectors, query_vectors
