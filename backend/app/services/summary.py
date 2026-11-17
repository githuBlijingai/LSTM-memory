from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

TEMPLATE_SUMMARY_VERSION = "template-v1"
IMPORTANT_EVENT_THRESHOLD = 0.7
FAMILIAR_EVENT_THRESHOLD = 1.0
RECENT_EVENT_LIMIT = 3
IMPORTANT_EVENT_LIMIT = 3
FAMILIAR_EVENT_LIMIT = 3
MAX_SUMMARY_EVENTS = 8


@dataclass(frozen=True)
class SummaryEvent:
    event_id: UUID
    title: str
    event_type: str
    event_time: datetime
    importance: float = 0.5
    familiarity_score: float = 0.0


@dataclass(frozen=True)
class SummaryResult:
    text: str
    source_event_ids: list[str]
    version: str


class SummaryProvider(Protocol):
    version: str

    def summarize(self, entity_name: str, events: Sequence[SummaryEvent]) -> SummaryResult:
        """生成实体可读摘要与来源事件 ID。"""


def _event_order(event: SummaryEvent) -> tuple[datetime, str]:
    return event.event_time, str(event.event_id)


class TemplateSummaryProvider:
    """首版确定性中文模板摘要，不调用外部 LLM。"""

    version = TEMPLATE_SUMMARY_VERSION

    def summarize(self, entity_name: str, events: Sequence[SummaryEvent]) -> SummaryResult:
        if not events:
            return SummaryResult(
                text=f"{entity_name} 暂无可用记忆。",
                source_event_ids=[],
                version=self.version,
            )
        ordered = sorted(events, key=_event_order, reverse=True)
        recent = ordered[:RECENT_EVENT_LIMIT]
        important = sorted(
            (item for item in ordered if item.importance >= IMPORTANT_EVENT_THRESHOLD),
            key=lambda item: (-item.importance, _event_order(item)),
        )[:IMPORTANT_EVENT_LIMIT]
        familiar = sorted(
            (item for item in ordered if item.familiarity_score >= FAMILIAR_EVENT_THRESHOLD),
            key=lambda item: (-item.familiarity_score, _event_order(item)),
        )[:FAMILIAR_EVENT_LIMIT]

        selected: list[SummaryEvent] = []
        seen: set[UUID] = set()
        for item in [*recent, *important, *familiar]:
            if item.event_id in seen:
                continue
            seen.add(item.event_id)
            selected.append(item)
            if len(selected) >= MAX_SUMMARY_EVENTS:
                break

        parts = [
            f"{item.title}（{item.event_type}，{item.event_time.date().isoformat()}）"
            for item in selected
        ]
        return SummaryResult(
            text=f"{entity_name} 的记忆摘要：" + "；".join(parts),
            source_event_ids=[str(item.event_id) for item in selected],
            version=self.version,
        )
