"""训练数据集：记录解析、按实体切分、时间截断和上下文窗口。

设计 4.4 要求：
1. 按实体划分训练/验证/测试集合，再按时间截断，禁止同一实体未来事件泄漏到训练集。
2. 删除或隐私擦除的数据不得进入新训练集。

本模块只做数据组织，不依赖 PyTorch，便于单元测试。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID


class DatasetError(ValueError):
    """训练数据格式或内容不合法。"""


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetError(f"{field_name} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetError(f"{field_name} 不是合法时间：{value}") from exc
    if parsed.tzinfo is None:
        raise DatasetError(f"{field_name} 必须包含时区")
    return parsed


def _parse_uuid(value: Any, field_name: str) -> UUID:
    if not isinstance(value, str):
        raise DatasetError(f"{field_name} 必须是 UUID 字符串")
    try:
        return UUID(value)
    except ValueError as exc:
        raise DatasetError(f"{field_name} 不是合法 UUID：{value}") from exc


def _parse_vector(value: Any, field_name: str) -> tuple[float, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise DatasetError(f"{field_name} 必须是非空数值数组")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise DatasetError(f"{field_name} 包含非数值")
        vector.append(float(item))
    return tuple(vector)


def _parse_bool(value: Any, field_name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DatasetError(f"{field_name} 必须是布尔值")
    return value


@dataclass(frozen=True)
class EventRecord:
    """训练用事件记录；`deleted` 与 `privacy_erased` 的数据不会进入训练集。"""

    event_id: UUID
    entity_id: UUID
    text: str
    event_time: datetime
    sequence_no: int | None = None
    deleted: bool = False
    privacy_erased: bool = False
    base_vector: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise DatasetError("事件文本不能为空")
        if self.event_time.tzinfo is None:
            raise DatasetError("event_time 必须包含时区")

    @property
    def usable(self) -> bool:
        return not self.deleted and not self.privacy_erased

    @property
    def sort_key(self) -> tuple[datetime, int, str]:
        return (
            self.event_time,
            self.sequence_no if self.sequence_no is not None else -1,
            str(self.event_id),
        )


@dataclass(frozen=True)
class QueryRecord:
    """查询记录；`relevant_event_ids` 来自有效正反馈。"""

    query_id: UUID
    text: str
    entity_id: UUID
    relevant_event_ids: tuple[UUID, ...]
    negative_event_ids: tuple[UUID, ...] = ()
    time_sensitive: bool = False
    cold_start: bool = False
    long_sequence: bool = False
    created_at: datetime | None = None
    base_vector: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise DatasetError("查询文本不能为空")
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise DatasetError("created_at 必须包含时区")


@dataclass(frozen=True)
class TrainingDataset:
    events: dict[UUID, EventRecord]
    queries: tuple[QueryRecord, ...]

    def usable_events(self) -> dict[UUID, EventRecord]:
        return {event_id: event for event_id, event in self.events.items() if event.usable}


@dataclass(frozen=True)
class DatasetSplit:
    train: TrainingDataset
    validation: TrainingDataset
    test: TrainingDataset
    cutoff: datetime | None
    train_entities: tuple[UUID, ...]
    validation_entities: tuple[UUID, ...]
    test_entities: tuple[UUID, ...]


@dataclass(frozen=True)
class TrainingPair:
    query_id: UUID
    positive_event_id: UUID
    negative_event_ids: tuple[UUID, ...] = ()


def parse_dataset(raw: Any) -> TrainingDataset:
    if not isinstance(raw, dict):
        raise DatasetError("训练数据根节点必须是对象")
    raw_events = raw.get("events")
    raw_queries = raw.get("queries")
    if not isinstance(raw_events, list) or not isinstance(raw_queries, list):
        raise DatasetError("训练数据必须包含 events 和 queries 数组")

    events: dict[UUID, EventRecord] = {}
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            raise DatasetError(f"events[{index}] 必须是对象")
        event = EventRecord(
            event_id=_parse_uuid(item.get("event_id"), f"events[{index}].event_id"),
            entity_id=_parse_uuid(item.get("entity_id"), f"events[{index}].entity_id"),
            text=str(item.get("text") or ""),
            event_time=_parse_datetime(item.get("event_time"), f"events[{index}].event_time"),
            sequence_no=item.get("sequence_no"),
            deleted=_parse_bool(item.get("deleted"), f"events[{index}].deleted"),
            privacy_erased=_parse_bool(
                item.get("privacy_erased"), f"events[{index}].privacy_erased"
            ),
            base_vector=_parse_vector(item.get("base_vector"), f"events[{index}].base_vector"),
        )
        if event.event_id in events:
            raise DatasetError(f"事件 ID 重复：{event.event_id}")
        events[event.event_id] = event

    queries: list[QueryRecord] = []
    seen_queries: set[UUID] = set()
    for index, item in enumerate(raw_queries):
        if not isinstance(item, dict):
            raise DatasetError(f"queries[{index}] 必须是对象")
        query_id = _parse_uuid(item.get("query_id"), f"queries[{index}].query_id")
        if query_id in seen_queries:
            raise DatasetError(f"查询 ID 重复：{query_id}")
        seen_queries.add(query_id)
        positives = item.get("relevant_event_ids")
        negatives = item.get("negative_event_ids") or []
        if not isinstance(positives, list) or not positives:
            raise DatasetError(f"queries[{index}].relevant_event_ids 必须是非空数组")
        if not isinstance(negatives, list):
            raise DatasetError(f"queries[{index}].negative_event_ids 必须是数组")
        created_at = item.get("created_at")
        queries.append(
            QueryRecord(
                query_id=query_id,
                text=str(item.get("text") or ""),
                entity_id=_parse_uuid(item.get("entity_id"), f"queries[{index}].entity_id"),
                relevant_event_ids=tuple(
                    _parse_uuid(value, f"queries[{index}].relevant_event_ids[]")
                    for value in positives
                ),
                negative_event_ids=tuple(
                    _parse_uuid(value, f"queries[{index}].negative_event_ids[]")
                    for value in negatives
                ),
                time_sensitive=_parse_bool(
                    item.get("time_sensitive"), f"queries[{index}].time_sensitive"
                ),
                cold_start=_parse_bool(item.get("cold_start"), f"queries[{index}].cold_start"),
                long_sequence=_parse_bool(
                    item.get("long_sequence"), f"queries[{index}].long_sequence"
                ),
                created_at=(
                    _parse_datetime(created_at, f"queries[{index}].created_at")
                    if created_at is not None
                    else None
                ),
                base_vector=_parse_vector(item.get("base_vector"), f"queries[{index}].base_vector"),
            )
        )
    return TrainingDataset(events=events, queries=tuple(queries))


def load_dataset(path: str | Path) -> TrainingDataset:
    dataset_path = Path(path)
    try:
        raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"训练数据无法读取：{dataset_path}") from exc
    except ValueError as exc:
        raise DatasetError(f"训练数据不是合法 JSON：{dataset_path}") from exc
    return parse_dataset(raw)


def dataset_to_dict(dataset: TrainingDataset) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def vector(value: tuple[float, ...] | None) -> list[float] | None:
        return list(value) if value else None

    return {
        "events": [
            {
                "event_id": str(event.event_id),
                "entity_id": str(event.entity_id),
                "text": event.text,
                "event_time": iso(event.event_time),
                "sequence_no": event.sequence_no,
                "deleted": event.deleted,
                "privacy_erased": event.privacy_erased,
                "base_vector": vector(event.base_vector),
            }
            for event in dataset.events.values()
        ],
        "queries": [
            {
                "query_id": str(query.query_id),
                "entity_id": str(query.entity_id),
                "text": query.text,
                "relevant_event_ids": [str(value) for value in query.relevant_event_ids],
                "negative_event_ids": [str(value) for value in query.negative_event_ids],
                "time_sensitive": query.time_sensitive,
                "cold_start": query.cold_start,
                "long_sequence": query.long_sequence,
                "created_at": iso(query.created_at),
                "base_vector": vector(query.base_vector),
            }
            for query in dataset.queries
        ],
    }


def save_dataset(dataset: TrainingDataset, path: str | Path) -> Path:
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(
        json.dumps(dataset_to_dict(dataset), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dataset_path


def _quantile(values: list[datetime], ratio: float) -> datetime | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * ratio)))
    return ordered[index]


def _subset(
    dataset: TrainingDataset,
    entities: frozenset[UUID],
    *,
    cutoff: datetime | None,
    keep_after_cutoff: bool,
) -> TrainingDataset:
    events: dict[UUID, EventRecord] = {}
    for event_id, event in dataset.usable_events().items():
        if event.entity_id not in entities:
            continue
        if cutoff is not None:
            if keep_after_cutoff and event.event_time <= cutoff:
                continue
            if not keep_after_cutoff and event.event_time > cutoff:
                continue
        events[event_id] = event
    queries = tuple(
        query
        for query in dataset.queries
        if query.entity_id in entities
        and any(event_id in events for event_id in query.relevant_event_ids)
    )
    return TrainingDataset(events=events, queries=queries)


def _take_counts(total: int, val_ratio: float, test_ratio: float) -> tuple[int, int, int]:
    if total <= 0:
        return 0, 0, 0
    if total == 1:
        return 1, 0, 0
    if total == 2:
        return 1, 1, 0
    test_count = max(1, round(total * test_ratio))
    val_count = max(1, round(total * val_ratio))
    train_count = total - test_count - val_count
    while train_count < 1:
        if test_count >= val_count and test_count > 1:
            test_count -= 1
        elif val_count > 1:
            val_count -= 1
        else:
            break
        train_count = total - test_count - val_count
    return train_count, val_count, test_count


def split_dataset(
    dataset: TrainingDataset,
    *,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    cutoff_ratio: float = 0.8,
    seed: int = 13,
) -> DatasetSplit:
    """按实体切分，再用时间截断：训练用截断前事件，验证/测试用截断后事件。"""
    if not 0 <= val_ratio < 1 or not 0 <= test_ratio < 1 or val_ratio + test_ratio >= 1:
        raise DatasetError("val_ratio 与 test_ratio 必须非负且之和小于 1")
    if not 0 < cutoff_ratio <= 1:
        raise DatasetError("cutoff_ratio 必须在 (0, 1] 内")

    usable = dataset.usable_events()
    entities = sorted({event.entity_id for event in usable.values()}, key=str)
    rng = random.Random(seed)
    rng.shuffle(entities)

    train_count, val_count, test_count = _take_counts(len(entities), val_ratio, test_ratio)
    train_entities = tuple(entities[:train_count])
    validation_entities = tuple(entities[train_count : train_count + val_count])
    test_entities = tuple(entities[train_count + val_count : train_count + val_count + test_count])
    cutoff = _quantile([event.event_time for event in usable.values()], cutoff_ratio)

    return DatasetSplit(
        train=_subset(dataset, frozenset(train_entities), cutoff=cutoff, keep_after_cutoff=False),
        validation=_subset(
            dataset, frozenset(validation_entities), cutoff=cutoff, keep_after_cutoff=True
        ),
        test=_subset(dataset, frozenset(test_entities), cutoff=cutoff, keep_after_cutoff=True),
        cutoff=cutoff,
        train_entities=train_entities,
        validation_entities=validation_entities,
        test_entities=test_entities,
    )


def build_windows(
    dataset: TrainingDataset, *, window_size: int = 8
) -> dict[UUID, tuple[UUID, ...]]:
    """为每个事件构建以其结尾的上下文窗口，窗口长度包含当前事件。"""
    if window_size < 1:
        raise DatasetError("window_size 必须大于等于 1")
    by_entity: dict[UUID, list[EventRecord]] = {}
    for event in dataset.usable_events().values():
        by_entity.setdefault(event.entity_id, []).append(event)

    windows: dict[UUID, tuple[UUID, ...]] = {}
    for items in by_entity.values():
        items.sort(key=lambda event: event.sort_key)
        ordered_ids = [event.event_id for event in items]
        for index, event_id in enumerate(ordered_ids):
            start = max(0, index - window_size + 1)
            windows[event_id] = tuple(ordered_ids[start : index + 1])
    return windows


def build_training_pairs(dataset: TrainingDataset) -> list[TrainingPair]:
    """把查询展开为查询—正例事件对，硬负例仅保留仍可用的事件。"""
    events = dataset.usable_events()
    pairs: list[TrainingPair] = []
    for query in dataset.queries:
        positives = [event_id for event_id in query.relevant_event_ids if event_id in events]
        negatives = tuple(event_id for event_id in query.negative_event_ids if event_id in events)
        for positive in positives:
            pairs.append(
                TrainingPair(
                    query_id=query.query_id,
                    positive_event_id=positive,
                    negative_event_ids=negatives,
                )
            )
    return pairs
