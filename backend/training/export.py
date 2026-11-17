"""从权威数据库导出训练数据：仅 ACTIVE 事件与计入熟悉度的 helpful 反馈。

设计 4.4 约束：
1. 删除或隐私擦除的数据不得进入训练集（PURGE 会清空 raw_content，因此天然被排除）。
2. 负反馈只作为质量样本，不自动当成绝对负例，因此这里不导出 negative_event_ids。

多实体查询的 `entity_id` 取排名最高的正例事件所属实体，用于实体级切分。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db import get_session_factory
from app.models import (
    EventMemory,
    EventStatus,
    FeedbackType,
    MemoryAccessLog,
    MemoryQuery,
    MemoryType,
)
from training.dataset import EventRecord, QueryRecord, TrainingDataset, save_dataset


@dataclass
class _QueryGroup:
    text: str
    created_at: datetime | None
    relevant: dict[UUID, int] = field(default_factory=dict)
    entity_id: UUID | None = None
    best_rank: int | None = None


def export_dataset(session: Session) -> TrainingDataset:
    events = {
        event.event_id: EventRecord(
            event_id=event.event_id,
            entity_id=event.entity_id,
            text=f"{event.title}\n{event.raw_content or ''}".strip(),
            event_time=event.event_time,
            sequence_no=event.sequence_no,
            deleted=event.status == EventStatus.PURGED,
            privacy_erased=not bool(event.raw_content),
        )
        for event in session.execute(
            select(EventMemory).where(
                EventMemory.status == EventStatus.ACTIVE,
                EventMemory.raw_content.is_not(None),
            )
        ).scalars()
    }

    rows = session.execute(
        select(
            MemoryAccessLog.query_id,
            MemoryAccessLog.memory_id,
            MemoryAccessLog.result_rank,
            MemoryQuery.query_text_redacted,
            MemoryQuery.created_at,
        )
        .join(MemoryQuery, MemoryQuery.query_id == MemoryAccessLog.query_id)
        .where(
            MemoryAccessLog.memory_type == MemoryType.EVENT,
            MemoryAccessLog.feedback_type == FeedbackType.HELPFUL,
            MemoryAccessLog.is_counted.is_(True),
        )
        .order_by(MemoryAccessLog.query_id, MemoryAccessLog.result_rank)
    ).all()

    grouped: dict[UUID, _QueryGroup] = {}
    for query_id, memory_id, rank, text, created_at in rows:
        event = events.get(memory_id)
        if event is None:
            continue
        group = grouped.setdefault(query_id, _QueryGroup(text=text, created_at=created_at))
        group.relevant[memory_id] = rank
        if group.best_rank is None or rank < group.best_rank:
            group.best_rank = rank
            group.entity_id = event.entity_id

    queries = [
        QueryRecord(
            query_id=query_id,
            text=group.text,
            entity_id=group.entity_id,
            relevant_event_ids=tuple(group.relevant),
            created_at=group.created_at,
        )
        for query_id, group in grouped.items()
        if group.entity_id is not None
    ]
    return TrainingDataset(events=events, queries=tuple(queries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从数据库导出训练数据")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args(argv)

    settings = Settings()
    if not settings.database_url:
        print(json.dumps({"ok": False, "error": "DATABASE_URL 未配置"}, ensure_ascii=False))
        return 2
    with get_session_factory(settings.database_url)() as session:
        dataset = export_dataset(session)
    path = save_dataset(dataset, Path(args.output))
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(path),
                "events": len(dataset.events),
                "queries": len(dataset.queries),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
