"""评估用向量与精排代理。

评估默认不依赖任何外部模型：
- 向量：字符 n-gram 带符号哈希（1024 维，L2 归一化），确定性、可离线复现。
- 精排：基于语料 IDF 的字符 n-gram 余弦重叠，模拟交叉编码器的相关性打分。

它们只用于建立**可重复的下限基线**，不代表 BGE / LSTM 的真实效果。若配置了
``EMBEDDING_PROVIDER``，可用 ``--embedding real`` 切换到运行期同款 Provider，
并在报告中记录实际模型版本。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

DEFAULT_DIM = 1024
ASCII_WORD = re.compile(r"[a-z0-9_]+")
ASCII_WORD_PATTERN = re.compile(r"[a-zA-Z0-9_]+")
CJK_SEGMENT = re.compile(r"[\u4e00-\u9fff]+")


def _features(text: str) -> Counter[str]:
    features: Counter[str] = Counter()
    lowered = text.lower()
    for word in ASCII_WORD.findall(lowered):
        features[f"w:{word}"] += 1.5
    for segment in CJK_SEGMENT.findall(text):
        if len(segment) == 1:
            features[f"u:{segment}"] += 0.5
            continue
        for index in range(len(segment) - 1):
            features[f"b:{segment[index:index + 2]}"] += 1.0
        for index in range(len(segment) - 2):
            features[f"t:{segment[index:index + 3]}"] += 1.2
    return features


def _sign_index(feature: str) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % DEFAULT_DIM
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class HashingEmbedding:
    """确定性词法向量；接口与运行期 EmbeddingProvider 保持一致。"""

    model_version = "eval-hash-ngram-v1"
    dimension = DEFAULT_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * DEFAULT_DIM
        for feature, weight in _features(text).items():
            index, sign = _sign_index(feature)
            vector[index] += sign * weight
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


class RealEmbedding:
    """复用运行期 Provider；仅在显式选择 ``--embedding real`` 时加载。"""

    def __init__(self) -> None:
        from app.core.config import Settings
        from training.vectors import build_embedding_provider

        settings = Settings()
        if not settings.embedding_provider:
            raise RuntimeError(
                "未配置 EMBEDDING_PROVIDER，无法使用真实向量；请改用默认词法代理或先配置 .env.local"
            )
        self._provider = build_embedding_provider(settings)
        self.model_version = (
            f"{settings.embedding_provider}:{settings.embedding_model_version or ''}"
        )
        self.dimension = settings.base_embedding_dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self._provider.embed(list(texts))


class LexicalReranker:
    """IDF 加权的字符 n-gram 余弦重叠，输出 [0,1]。"""

    model_version = "eval-lexical-rerank-v1"

    def __init__(self, documents: Iterable[str] = ()) -> None:
        self._df: Counter[str] = Counter()
        self._documents = 0
        self.fit(documents)

    def fit(self, documents: Iterable[str]) -> None:
        seen: set[str] = set()
        count = 0
        for document in documents:
            count += 1
            for feature in _features(document):
                if feature not in seen:
                    seen.add(feature)
                    self._df[feature] += 1
            seen.clear()
        self._documents = count

    def _idf(self, feature: str) -> float:
        return math.log((1 + self._documents) / (1 + self._df.get(feature, 0))) + 1.0

    def score(self, query: str, document: str) -> float:
        query_features = _features(query)
        doc_features = _features(document)
        if not query_features or not doc_features:
            return 0.0
        shared = set(query_features) & set(doc_features)
        if not shared:
            return 0.0
        numerator = sum(self._idf(feature) for feature in shared)
        query_norm = math.sqrt(sum(self._idf(feature) for feature in query_features))
        doc_norm = math.sqrt(sum(self._idf(feature) for feature in doc_features))
        if query_norm == 0 or doc_norm == 0:
            return 0.0
        score = numerator / (query_norm * doc_norm)
        if _longest_common_substring(query, document) >= 4:
            score = min(1.0, score + 0.1)
        return max(0.0, min(1.0, score))

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        return [self.score(query, document) for document in documents]


def _longest_common_substring(left: str, right: str) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def build_embedding(name: str) -> HashingEmbedding | RealEmbedding:
    if name == "hash":
        return HashingEmbedding()
    if name == "real":
        return RealEmbedding()
    raise ValueError(f"未知向量后端：{name}")
