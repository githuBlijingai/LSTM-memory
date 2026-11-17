from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.sequence import (
    WindowEvent,
    build_window,
    is_late_event,
    left_padded_mask,
    pad_window_vectors,
    window_start_index,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _event(hours_ago: int, value: int) -> WindowEvent:
    from uuid import UUID

    return WindowEvent(
        event_id=UUID(int=value),
        event_time=NOW - timedelta(hours=hours_ago),
    )


def test_window_shorter_than_k_left_pads_and_masks() -> None:
    window = build_window([_event(3, 1), _event(2, 2), _event(1, 3)], window_size=4)
    assert window.length == 4
    assert window.valid_count == 3
    assert window.events[0] is None
    assert window.mask == (0.0, 1.0, 1.0, 1.0)
    assert [event.event_id.int for event in window.events if event] == [1, 2, 3]


def test_window_exactly_k_has_no_padding() -> None:
    window = build_window([_event(hour, hour) for hour in range(4, 0, -1)], window_size=4)
    assert window.events[0] is not None
    assert window.mask == (1.0, 1.0, 1.0, 1.0)
    assert not window.is_padded


def test_window_over_k_keeps_latest_events() -> None:
    events = [_event(hour, hour) for hour in range(10, 0, -1)]
    window = build_window(events, window_size=4)
    assert window.valid_count == 4
    assert [event.event_id.int for event in window.events if event] == [4, 3, 2, 1]


def test_window_tie_breaks_by_event_id() -> None:
    first = _event(1, 1)
    second = _event(1, 2)
    window = build_window([second, first], window_size=2)
    assert [event.event_id.int for event in window.events if event] == [1, 2]


def test_late_event_detection() -> None:
    assert is_late_event(NOW - timedelta(hours=1), NOW)
    assert not is_late_event(NOW, NOW)
    assert not is_late_event(NOW, None)


def test_window_helpers_validate_inputs() -> None:
    assert window_start_index(3, 4) == 0
    assert window_start_index(10, 4) == 6
    assert left_padded_mask(2, 4) == (0.0, 0.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        window_start_index(3, 0)
    with pytest.raises(ValueError):
        left_padded_mask(5, 4)


def test_pad_window_vectors_aligns_with_mask() -> None:
    padded = pad_window_vectors([[1.0, 2.0], [3.0, 4.0]], window_size=4)
    assert padded == [[0.0, 0.0], [0.0, 0.0], [1.0, 2.0], [3.0, 4.0]]
    assert len(padded) == len(left_padded_mask(2, 4))
    assert pad_window_vectors([[1.0]], window_size=1) == [[1.0]]
    with pytest.raises(ValueError):
        pad_window_vectors([[1.0]], window_size=0)
    with pytest.raises(ValueError):
        pad_window_vectors([[1.0], [2.0]], window_size=1)
