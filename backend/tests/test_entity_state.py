from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.state import (
    SnapshotRef,
    can_replay,
    next_sequence_no,
    next_state_version,
    plan_replay,
    select_rebuild_snapshot,
)
from app.ml.event_encoder import EncodedEvent
from app.ml.serialization import SerializedState, compute_checksum
from app.models import SnapshotKind
from app.services.entity_state import EntityStateService, StateCache, TensorState

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _serialized() -> SerializedState:
    return SerializedState(
        h_bytes=b"stub-h",
        c_bytes=b"stub-c",
        dtype="stub",
        shape=[1, 4],
        checksum=compute_checksum(b"stub-h", b"stub-c"),
    )


class StubEncoder:
    """测试专用确定性编码器，不进入生产默认路径。"""

    model_version = "stub-v1"

    def encode(self, base_vectors, *, mask=None, init_state=None) -> EncodedEvent:
        assert base_vectors
        return EncodedEvent(event_vector=[1.0, 0.0], state=_serialized())


def _service() -> EntityStateService:
    return EntityStateService(
        None,  # type: ignore[arg-type]
        Settings(environment="test"),
        encoder=StubEncoder(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        decode_state=lambda serialized: ([0.0], [0.0]),
        cache=StateCache(None),
    )


def _previous(
    sequence_no: int = 7, version: int = 3, model_version: str = "stub-v1"
) -> TensorState:
    return TensorState(
        state_version=version,
        last_sequence_no=sequence_no,
        last_event_id=UUID(int=1),
        model_version=model_version,
        serialized=_serialized(),
    )


def test_replay_limit_is_two() -> None:
    assert can_replay(1, max_count=2)
    assert not can_replay(2, max_count=2)
    with pytest.raises(ValueError):
        plan_replay(
            current_state_version=3,
            last_sequence_no=7,
            replay_count=2,
            max_count=2,
        )
    assert next_state_version(3) == 4
    assert next_sequence_no(7) == 8


def test_replay_transition_does_not_change_last_sequence_no() -> None:
    service = _service()
    transition = service.apply_transition(
        previous=_previous(sequence_no=7, version=3),
        base_vectors=[[0.0] * 4],
        mask=[1.0],
        init_state=([0.0], [0.0]),
        state_version=4,
        last_sequence_no=7,
        last_event_id=UUID(int=2),
        model_version="stub-v1",
        snapshot_kind=SnapshotKind.REPLAY,
    )
    assert transition.state.state_version == 4
    assert transition.state.last_sequence_no == 7
    assert transition.state.last_event_id == UUID(int=2)
    assert transition.event_vector == [1.0, 0.0]


def test_replay_transition_rejects_sequence_change() -> None:
    service = _service()
    with pytest.raises(AppError) as error:
        service.apply_transition(
            previous=_previous(sequence_no=7, version=3),
            base_vectors=[[0.0] * 4],
            mask=[1.0],
            init_state=None,
            state_version=4,
            last_sequence_no=8,
            last_event_id=UUID(int=2),
            model_version="stub-v1",
            snapshot_kind=SnapshotKind.REPLAY,
        )
    assert error.value.code == "REPLAY_INVALID"


def test_transition_rejects_model_version_mismatch() -> None:
    service = _service()
    with pytest.raises(AppError) as error:
        service.apply_transition(
            previous=_previous(model_version="stub-v1"),
            base_vectors=[[0.0] * 4],
            mask=[1.0],
            init_state=None,
            state_version=4,
            last_sequence_no=8,
            last_event_id=UUID(int=2),
            model_version="stub-v2",
            snapshot_kind=SnapshotKind.UPDATE,
        )
    assert error.value.code == "STATE_MODEL_MISMATCH"


def test_snapshot_selection_requires_matching_model_and_prior_sequence() -> None:
    candidates = [
        SnapshotRef(UUID(int=1), state_version=1, model_version="stub-v1", last_sequence_no=3),
        SnapshotRef(UUID(int=2), state_version=2, model_version="stub-v1", last_sequence_no=7),
        SnapshotRef(UUID(int=3), state_version=3, model_version="stub-v1", last_sequence_no=9),
        SnapshotRef(UUID(int=4), state_version=4, model_version="stub-v0", last_sequence_no=8),
    ]
    selected = select_rebuild_snapshot(candidates, model_version="stub-v1", change_sequence_no=9)
    assert selected is not None
    assert selected.snapshot_id == UUID(int=2)
    assert selected.last_sequence_no == 7

    assert (
        select_rebuild_snapshot(candidates, model_version="stub-v1", change_sequence_no=3) is None
    )
    assert (
        select_rebuild_snapshot(candidates, model_version="stub-v2", change_sequence_no=9) is None
    )
