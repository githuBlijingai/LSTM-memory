from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: UUID
    state_version: int
    model_version: str
    last_sequence_no: int


@dataclass(frozen=True)
class ReplayTransition:
    state_version: int
    last_sequence_no: int


def next_state_version(current_state_version: int) -> int:
    if current_state_version < 0:
        raise ValueError("current_state_version must be non-negative")
    return current_state_version + 1


def next_sequence_no(last_sequence_no: int) -> int:
    if last_sequence_no < 0:
        raise ValueError("last_sequence_no must be non-negative")
    return last_sequence_no + 1


def can_replay(replay_count: int, *, max_count: int) -> bool:
    if replay_count < 0:
        raise ValueError("replay_count must be non-negative")
    if max_count < 0:
        raise ValueError("max_count must be non-negative")
    return replay_count < max_count


def plan_replay(
    *,
    current_state_version: int,
    last_sequence_no: int,
    replay_count: int,
    max_count: int,
) -> ReplayTransition:
    """重放推进 state_version，但不改变 last_sequence_no。"""
    if not can_replay(replay_count, max_count=max_count):
        raise ValueError("replay limit reached")
    return ReplayTransition(
        state_version=next_state_version(current_state_version),
        last_sequence_no=last_sequence_no,
    )


def snapshot_is_compatible(snapshot: SnapshotRef, *, model_version: str) -> bool:
    return snapshot.model_version == model_version


def snapshot_is_before(snapshot: SnapshotRef, *, change_sequence_no: int) -> bool:
    return snapshot.last_sequence_no < change_sequence_no


def select_rebuild_snapshot(
    candidates: Iterable[SnapshotRef],
    *,
    model_version: str,
    change_sequence_no: int,
) -> SnapshotRef | None:
    """选择模型一致且严格早于变更点的最近快照。"""
    eligible = [
        snapshot
        for snapshot in candidates
        if snapshot_is_compatible(snapshot, model_version=model_version)
        and snapshot_is_before(snapshot, change_sequence_no=change_sequence_no)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.last_sequence_no, item.state_version))
