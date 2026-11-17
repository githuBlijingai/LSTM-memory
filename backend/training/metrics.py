"""检索评估指标：Recall@K、MRR@K、nDCG@K 与发布门槛检查。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

DEFAULT_K = 10
METRIC_NAMES = ("recall", "mrr", "ndcg")


@dataclass(frozen=True)
class RankedCase:
    """单条查询的排序结果；`ranked_ids` 已按分数从高到低排列。"""

    ranked_ids: tuple[UUID, ...]
    relevant_ids: frozenset[UUID]
    time_sensitive: bool = False
    cold_start: bool = False
    long_sequence: bool = False


def recall_at_k(ranked_ids: Sequence[UUID], relevant_ids: Iterable[UUID], k: int) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    hits = len(set(ranked_ids[:k]) & relevant)
    return hits / len(relevant)


def mrr_at_k(ranked_ids: Sequence[UUID], relevant_ids: Iterable[UUID], k: int) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    for index, memory_id in enumerate(ranked_ids[:k]):
        if memory_id in relevant:
            return 1.0 / (index + 1)
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[UUID], relevant_ids: Iterable[UUID], k: int) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    dcg = 0.0
    for index, memory_id in enumerate(ranked_ids[:k]):
        if memory_id in relevant:
            dcg += 1.0 / math.log2(index + 2)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(relevant), k)))
    if ideal == 0:
        return 0.0
    return dcg / ideal


def _aggregate(cases: Sequence[RankedCase], k: int) -> dict[str, Any]:
    if not cases:
        return {"cases": 0}
    total = len(cases)
    return {
        "cases": total,
        "recall": sum(recall_at_k(case.ranked_ids, case.relevant_ids, k) for case in cases) / total,
        "mrr": sum(mrr_at_k(case.ranked_ids, case.relevant_ids, k) for case in cases) / total,
        "ndcg": sum(ndcg_at_k(case.ranked_ids, case.relevant_ids, k) for case in cases) / total,
    }


def evaluate_cases(
    cases: Sequence[RankedCase],
    *,
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """返回整体与分组指标；分组按设计 4.4 的发布门槛报告要求。"""
    if k < 1:
        raise ValueError("k 必须大于等于 1")
    report: dict[str, Any] = {"k": k, "all": _aggregate(cases, k)}
    groups = (
        ("time_sensitive", "time_sensitive"),
        ("cold_start", "cold_start"),
        ("long_sequence", "long_sequence"),
    )
    for name, attribute in groups:
        subset = [case for case in cases if getattr(case, attribute)]
        if subset:
            report[name] = _aggregate(subset, k)
    return report


def gate_failures(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    *,
    tolerance: float = 1e-9,
) -> list[str]:
    """候选模型任一指标低于纯 BGE 基线即视为未通过发布门槛。"""
    failures: list[str] = []
    candidate_all = candidate.get("all", {})
    baseline_all = baseline.get("all", {})
    for name in METRIC_NAMES:
        candidate_value = float(candidate_all.get(name, 0.0))
        baseline_value = float(baseline_all.get(name, 0.0))
        if candidate_value + tolerance < baseline_value:
            failures.append(f"{name} {candidate_value:.4f} < 基线 {baseline_value:.4f}")
    return failures
