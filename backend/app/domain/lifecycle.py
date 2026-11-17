from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.scoring import delete_score


@dataclass(frozen=True)
class ArchiveDecision:
    eligible: bool
    blockers: tuple[str, ...]

    @property
    def reason(self) -> str | None:
        return self.blockers[0] if self.blockers else None


def evaluate_auto_archive(
    *,
    decay_score_value: float,
    familiarity_score: float,
    created_at: datetime,
    clock: Callable[[], datetime],
    delete_threshold: float,
    protect_hours: int,
    protected: bool = False,
    pending_correction: bool = False,
    pending_rebuild: bool = False,
    compliance_hold: bool = False,
) -> ArchiveDecision:
    """判断事件是否满足自动归档条件，并返回全部阻塞原因。"""
    blockers: list[str] = []
    if delete_score(decay_score_value, familiarity_score) >= delete_threshold:
        blockers.append("delete_score")
    if protect_hours > 0 and clock() - created_at < timedelta(hours=protect_hours):
        blockers.append("new_memory")
    if protected:
        blockers.append("protected")
    if pending_correction:
        blockers.append("pending_correction")
    if pending_rebuild:
        blockers.append("pending_rebuild")
    if compliance_hold:
        blockers.append("compliance_hold")
    return ArchiveDecision(eligible=not blockers, blockers=tuple(blockers))


def purge_after(archived_at: datetime, *, retention_days: int) -> datetime:
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    return archived_at + timedelta(days=retention_days)


def is_purge_due(
    archived_at: datetime,
    *,
    clock: Callable[[], datetime],
    retention_days: int,
) -> bool:
    return clock() >= purge_after(archived_at, retention_days=retention_days)
