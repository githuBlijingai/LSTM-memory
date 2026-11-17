from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

MemoryType = Literal["event", "wiki"]
MAX_WIKI_CHUNKS_PER_ARTICLE = 2


@dataclass(frozen=True)
class RankedItem:
    """进入融合排序的最小候选结构，便于纯函数测试。"""

    memory_type: MemoryType
    memory_id: UUID
    final_score: float
    article_id: UUID | None = None
    tie_break: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def fuse_score(intent_weight: float, relevance: float, memory_weight: float) -> float:
    """F = W_type * r * M，三项均在 [0, 1] 内。"""
    return clamp(intent_weight) * clamp(relevance) * clamp(memory_weight)


def fuse_ranked(
    items: list[RankedItem],
    *,
    top_n: int,
    max_wiki_chunks_per_article: int = MAX_WIKI_CHUNKS_PER_ARTICLE,
) -> list[RankedItem]:
    """全局排序并按文章限制 Wiki 分块数量，同时保持稳定顺序。"""
    ordered = sorted(
        items,
        key=lambda item: (-item.final_score, item.memory_type, str(item.memory_id), item.tie_break),
    )
    kept: list[RankedItem] = []
    wiki_per_article: dict[UUID, int] = {}
    for item in ordered:
        if len(kept) >= top_n:
            break
        if item.memory_type == "wiki" and item.article_id is not None:
            used = wiki_per_article.get(item.article_id, 0)
            if used >= max_wiki_chunks_per_article:
                continue
            wiki_per_article[item.article_id] = used + 1
        kept.append(item)
    return kept


def daily_gain(remaining_cap: float, step: float) -> float:
    """单次有效反馈的熟悉度增量，受剩余每日上限约束。"""
    if remaining_cap <= 0:
        return 0.0
    return max(0.0, min(step, remaining_cap))
