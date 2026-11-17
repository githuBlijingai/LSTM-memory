from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class WindowEvent:
    event_id: UUID
    event_time: datetime


def deterministic_sort_key(event: WindowEvent) -> tuple[datetime, str]:
    """事件按 (event_time, event_id) 确定性排序。"""
    return event.event_time, str(event.event_id)


def is_late_event(event_time: datetime, last_event_time: datetime | None) -> bool:
    """新事件早于实体最后事件时间即为迟到事件。"""
    if last_event_time is None:
        return False
    return event_time < last_event_time


def window_start_index(total_events: int, window_size: int) -> int:
    if window_size < 1:
        raise ValueError("window_size must be positive")
    return max(0, total_events - window_size)


@dataclass(frozen=True)
class PaddedWindow:
    events: tuple[WindowEvent | None, ...]
    mask: tuple[float, ...]

    @property
    def length(self) -> int:
        return len(self.events)

    @property
    def valid_count(self) -> int:
        return sum(1 for value in self.mask if value > 0)

    @property
    def is_padded(self) -> bool:
        return self.valid_count < self.length


def left_padded_mask(valid_count: int, window_size: int) -> tuple[float, ...]:
    if window_size < 1:
        raise ValueError("window_size must be positive")
    if valid_count < 0 or valid_count > window_size:
        raise ValueError("valid_count must be within [0, window_size]")
    padding = window_size - valid_count
    return (0.0,) * padding + (1.0,) * valid_count


def pad_window_vectors(
    vectors: list[list[float]], *, window_size: int
) -> list[list[float]]:
    """左补零向量到窗口大小，与 `left_padded_mask` 的位置一一对应。"""
    if window_size < 1:
        raise ValueError("window_size must be positive")
    if len(vectors) > window_size:
        raise ValueError("vectors must not exceed window_size")
    dimension = len(vectors[0]) if vectors else 0
    padding = [[0.0] * dimension for _ in range(window_size - len(vectors))]
    return [*padding, *vectors]


def build_window(
    events: list[WindowEvent] | tuple[WindowEvent, ...],
    *,
    window_size: int,
) -> PaddedWindow:
    """按确定性顺序取最近 K 条事件并左补零；不足 K 条时 mask 标记有效位置。"""
    if window_size < 1:
        raise ValueError("window_size must be positive")
    ordered = sorted(events, key=deterministic_sort_key)
    selected = ordered[window_start_index(len(ordered), window_size) :]
    padding = window_size - len(selected)
    padded: tuple[WindowEvent | None, ...] = (None,) * padding + tuple(selected)
    mask = left_padded_mask(len(selected), window_size)
    return PaddedWindow(events=padded, mask=mask)
