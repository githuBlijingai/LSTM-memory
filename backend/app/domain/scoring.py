from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

SECONDS_PER_DAY = 86400.0
RELEVANCE_COSINE_WEIGHT = 0.4
RELEVANCE_RERANK_WEIGHT = 0.6
DELETE_FAMILIARITY_FACTOR = 0.3


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return max(lower, min(upper, value))


def cos_norm(cosine_similarity: float) -> float:
    """把原始余弦相似度压缩到 [0,1]。"""
    return clamp((cosine_similarity + 1) / 2)


def relevance(cosine_similarity: float, rerank_score: float | None = None) -> float:
    """候选相关性；精排关闭时直接使用归一化余弦。"""
    normalized = cos_norm(cosine_similarity)
    if rerank_score is None:
        return normalized
    return clamp(
        RELEVANCE_COSINE_WEIGHT * normalized + RELEVANCE_RERANK_WEIGHT * clamp(rerank_score)
    )


def age_days(created_at: datetime, *, clock: Callable[[], datetime]) -> float:
    return max(0.0, (clock() - created_at).total_seconds() / SECONDS_PER_DAY)


def event_base_decay(
    initial_decay_score: float,
    created_at: datetime,
    *,
    clock: Callable[[], datetime],
    decay_lambda: float,
) -> float:
    """D_event = initial_decay_score * exp(-DECAY_LAMBDA * age_days)。"""
    if initial_decay_score < 0:
        raise ValueError("initial_decay_score must be non-negative")
    if decay_lambda < 0:
        raise ValueError("decay_lambda must be non-negative")
    return initial_decay_score * math.exp(-decay_lambda * age_days(created_at, clock=clock))


@dataclass(frozen=True)
class FamiliarityDecay:
    score: float
    updated_at: datetime


def materialize_familiarity(
    familiarity_score: float,
    *,
    anchor: datetime | None,
    created_at: datetime,
    clock: Callable[[], datetime],
    gamma: float,
    max_score: float,
    protect_hours: int,
) -> FamiliarityDecay:
    """物化熟悉度衰减。

    - 48 小时保护期内不衰减，但锚点推进到当前时间，避免保护期结束后补算。
    - 未发生有效反馈时以 created_at 为初始锚点。
    - 同一时间锚点重复调用不会重复衰减。
    """
    if familiarity_score < 0:
        raise ValueError("familiarity_score must be non-negative")
    if gamma < 0:
        raise ValueError("gamma must be non-negative")
    now = clock()
    if protect_hours > 0 and now - created_at < timedelta(hours=protect_hours):
        return FamiliarityDecay(familiarity_score, now)
    effective_anchor = anchor or created_at
    if now <= effective_anchor:
        return FamiliarityDecay(familiarity_score, effective_anchor)
    elapsed_days = (now - effective_anchor).total_seconds() / SECONDS_PER_DAY
    decayed = familiarity_score * math.exp(-gamma * elapsed_days)
    return FamiliarityDecay(min(max_score, max(0.0, decayed)), now)


def event_memory_weight(
    decay_score: float,
    familiarity_score: float,
    *,
    cap: float,
    familiarity_beta: float,
) -> float:
    """W_event = min(D_event * (1 + beta * F_event), MEMORY_WEIGHT_CAP)。"""
    if decay_score < 0 or familiarity_score < 0:
        raise ValueError("decay and familiarity scores must be non-negative")
    return min(decay_score * (1 + familiarity_beta * familiarity_score), cap)


def wiki_memory_weight(
    importance: float,
    familiarity_score: float,
    *,
    cap: float,
    wiki_familiarity_alpha: float,
) -> float:
    """W_wiki = min(importance * (1 + alpha * F_wiki), MEMORY_WEIGHT_CAP)。"""
    if not 0 <= importance <= 1:
        raise ValueError("importance must be within [0,1]")
    if familiarity_score < 0:
        raise ValueError("familiarity_score must be non-negative")
    return min(importance * (1 + wiki_familiarity_alpha * familiarity_score), cap)


def delete_score(decay_score: float, familiarity_score: float) -> float:
    """delete_score = D_event * (1 + 0.3 * F_event)。"""
    if decay_score < 0 or familiarity_score < 0:
        raise ValueError("decay and familiarity scores must be non-negative")
    return decay_score * (1 + DELETE_FAMILIARITY_FACTOR * familiarity_score)
