from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.lifecycle import evaluate_auto_archive, is_purge_due, purge_after
from app.domain.scoring import (
    cos_norm,
    delete_score,
    event_base_decay,
    event_memory_weight,
    materialize_familiarity,
    relevance,
    retrieval_hit_delta,
    wiki_memory_weight,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def test_cos_norm_and_relevance() -> None:
    assert cos_norm(-1.0) == 0.0
    assert cos_norm(0.0) == pytest.approx(0.5)
    assert cos_norm(1.0) == 1.0
    assert relevance(0.0) == pytest.approx(0.5)
    assert relevance(1.0, 0.5) == pytest.approx(0.4 * 1.0 + 0.6 * 0.5)
    # Rerank 分数在领域层被钳制到 [0,1]，Provider 层负责拒绝越界原始分数。
    assert relevance(0.0, 1.5) == pytest.approx(0.4 * 0.5 + 0.6 * 1.0)


def test_event_base_decay_uses_created_at() -> None:
    created_at = NOW - timedelta(days=100)
    assert event_base_decay(1.0, created_at, clock=clock, decay_lambda=0.01) == pytest.approx(
        math.exp(-1.0)
    )
    assert event_base_decay(1.0, NOW, clock=clock, decay_lambda=0.01) == pytest.approx(1.0)


def test_familiarity_protected_for_48_hours() -> None:
    created_at = NOW - timedelta(hours=1)
    result = materialize_familiarity(
        1.0,
        anchor=created_at,
        created_at=created_at,
        clock=clock,
        gamma=0.003,
        max_score=2.0,
        protect_hours=48,
    )
    assert result.score == 1.0
    assert result.updated_at == NOW


def test_familiarity_same_anchor_does_not_double_decay() -> None:
    created_at = NOW - timedelta(hours=200)
    anchor = NOW - timedelta(hours=100)
    first = materialize_familiarity(
        1.0,
        anchor=anchor,
        created_at=created_at,
        clock=clock,
        gamma=0.003,
        max_score=2.0,
        protect_hours=48,
    )
    second = materialize_familiarity(
        first.score,
        anchor=first.updated_at,
        created_at=created_at,
        clock=clock,
        gamma=0.003,
        max_score=2.0,
        protect_hours=48,
    )
    assert first.score < 1.0
    assert second.score == pytest.approx(first.score)
    assert second.updated_at == first.updated_at


def test_familiarity_decays_after_protection_window() -> None:
    created_at = NOW - timedelta(days=10)
    anchor = NOW - timedelta(days=5)
    result = materialize_familiarity(
        1.0,
        anchor=anchor,
        created_at=created_at,
        clock=clock,
        gamma=0.003,
        max_score=2.0,
        protect_hours=48,
    )
    assert result.score <= 1.0
    assert result.score < 1.0


def test_retrieval_hit_delta_decreases_and_never_reaches_one() -> None:
    assert retrieval_hit_delta(0.0, 0.02) == pytest.approx(0.02)
    assert retrieval_hit_delta(0.5, 0.02) == pytest.approx(0.01)
    assert retrieval_hit_delta(0.9, 0.02) == pytest.approx(0.002)
    assert retrieval_hit_delta(1.0, 0.02) == 0.0
    assert retrieval_hit_delta(1.5, 0.02) == 0.0

    score = 0.0
    for _ in range(1000):
        score = min(1.0, score + retrieval_hit_delta(score, 0.02))
    assert score <= 1.0
    assert score < 1.0


def test_retrieval_hit_delta_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError):
        retrieval_hit_delta(-0.1, 0.02)
    with pytest.raises(ValueError):
        retrieval_hit_delta(0.1, -0.02)


def test_memory_weights_and_delete_score() -> None:
    assert event_memory_weight(1.0, 2.0, cap=2.5, familiarity_beta=0.5) == pytest.approx(2.0)
    assert event_memory_weight(2.0, 2.0, cap=2.5, familiarity_beta=0.5) == pytest.approx(2.5)
    assert wiki_memory_weight(1.0, 1.0, cap=2.5, wiki_familiarity_alpha=0.6) == pytest.approx(1.6)
    assert wiki_memory_weight(1.0, 2.0, cap=1.5, wiki_familiarity_alpha=0.6) == pytest.approx(1.5)
    assert delete_score(0.2, 2.0) == pytest.approx(0.2 * 1.6)


def test_archive_threshold_and_blockers() -> None:
    old = NOW - timedelta(days=100)
    eligible = evaluate_auto_archive(
        decay_score_value=0.05,
        familiarity_score=0.0,
        created_at=old,
        clock=clock,
        delete_threshold=0.1,
        protect_hours=48,
    )
    assert eligible.eligible
    assert eligible.blockers == ()

    above_threshold = evaluate_auto_archive(
        decay_score_value=0.2,
        familiarity_score=0.0,
        created_at=old,
        clock=clock,
        delete_threshold=0.1,
        protect_hours=48,
    )
    assert not above_threshold.eligible
    assert "delete_score" in above_threshold.blockers

    fresh = evaluate_auto_archive(
        decay_score_value=0.05,
        familiarity_score=0.0,
        created_at=NOW - timedelta(hours=1),
        clock=clock,
        delete_threshold=0.1,
        protect_hours=48,
    )
    assert "new_memory" in fresh.blockers

    protected = evaluate_auto_archive(
        decay_score_value=0.05,
        familiarity_score=0.0,
        created_at=old,
        clock=clock,
        delete_threshold=0.1,
        protect_hours=48,
        protected=True,
        pending_rebuild=True,
    )
    assert {"protected", "pending_rebuild"} <= set(protected.blockers)


def test_purge_schedule() -> None:
    archived_at = NOW - timedelta(days=31)
    assert purge_after(archived_at, retention_days=30) == archived_at + timedelta(days=30)
    assert is_purge_due(archived_at, clock=clock, retention_days=30)
    assert not is_purge_due(NOW, clock=clock, retention_days=30)
