from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Intent = Literal["event", "wiki", "mixed"]

# 规则分类器关键词：首版允许替换为模型分类器而不改变 API 契约。
EVENT_HINTS: tuple[str, ...] = (
    "上次",
    "之前",
    "当时",
    "什么时候",
    "哪天",
    "聊过",
    "说过",
    "提过",
    "会议",
    "纪要",
    "决定",
    "确定",
    "安排",
    "进度",
    "谁负责",
    "deadline",
    "还记得",
)
WIKI_HINTS: tuple[str, ...] = (
    "是什么",
    "什么是",
    "定义",
    "概念",
    "原理",
    "解释",
    "介绍",
    "百科",
    "区别",
    "含义",
    "如何理解",
    "怎么理解",
    "what is",
    "why",
)


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    reason: str
    event_weight: float
    wiki_weight: float


WEIGHTS: dict[Intent, tuple[float, float]] = {
    "event": (0.8, 0.2),
    "wiki": (0.2, 0.8),
    "mixed": (0.5, 0.5),
}
SINGLE_MEMORY_TYPE_WEIGHT = 1.0


def _count_hits(query: str, hints: tuple[str, ...]) -> int:
    lowered = query.lower()
    return sum(1 for hint in hints if hint in lowered)


def classify_intent(
    query: str,
    *,
    memory_type: Literal["all", "event", "wiki"] = "all",
    has_entity_filter: bool = False,
    has_time_filter: bool = False,
    has_wiki_filter: bool = False,
) -> IntentDecision:
    """按设计 4.8 的规则识别意图，理由随后写入查询日志。"""
    if memory_type == "event":
        return IntentDecision(
            "event",
            "memory_type=event，仅检索事件记忆",
            SINGLE_MEMORY_TYPE_WEIGHT,
            0.0,
        )
    if memory_type == "wiki":
        return IntentDecision(
            "wiki",
            "memory_type=wiki，仅检索 Wiki 记忆",
            0.0,
            SINGLE_MEMORY_TYPE_WEIGHT,
        )
    if has_wiki_filter:
        weight_event, weight_wiki = WEIGHTS["wiki"]
        return IntentDecision("wiki", "包含 Wiki 分类/语言过滤条件", weight_event, weight_wiki)
    if has_entity_filter or has_time_filter:
        weight_event, weight_wiki = WEIGHTS["event"]
        return IntentDecision(
            "event",
            "包含主体或时间过滤条件，倾向事件回忆",
            weight_event,
            weight_wiki,
        )
    event_hits = _count_hits(query, EVENT_HINTS)
    wiki_hits = _count_hits(query, WIKI_HINTS)
    if event_hits > wiki_hits:
        weight_event, weight_wiki = WEIGHTS["event"]
        return IntentDecision(
            "event",
            f"回忆表达命中 {event_hits} 个，知识表达命中 {wiki_hits} 个",
            weight_event,
            weight_wiki,
        )
    if wiki_hits > event_hits:
        weight_event, weight_wiki = WEIGHTS["wiki"]
        return IntentDecision(
            "wiki",
            f"知识表达命中 {wiki_hits} 个，回忆表达命中 {event_hits} 个",
            weight_event,
            weight_wiki,
        )
    weight_event, weight_wiki = WEIGHTS["mixed"]
    return IntentDecision(
        "mixed",
        f"事件与知识表达命中数相同（{event_hits}）或均未命中",
        weight_event,
        weight_wiki,
    )
