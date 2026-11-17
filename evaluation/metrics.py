"""离线检索指标（支持多级相关性标注）。

指标定义
--------
- Recall@K：前 K 个结果覆盖的相关记忆数 / 全部相关记忆数（相关性等级 > 0）。
- Precision@K：前 K 个结果中相关记忆数 / K。
- MRR@K：第一个相关结果排名的倒数，未命中为 0。
- MAP@K：每个相关结果命中位置的 precision 平均值，再对相关结果数归一化。
- Hit@K：前 K 个结果是否至少命中一个相关记忆（0/1）。
- nDCG@K：增益 gain = 2^rel - 1，折损 1/log2(rank+1)，用理想排序归一化。

相关性等级约定：2 = 直接答案，1 = 相关背景，0/未标注 = 无关。
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

PRIMARY_K = 10
REPORT_KS = (1, 5, 10)


@dataclass(frozen=True)
class RankedCase:
    """单条查询的排序结果；gains 为 memory_id -> 相关性等级。"""

    query_key: str
    ranked_ids: tuple[str, ...]
    gains: dict[str, int]
    slices: tuple[str, ...] = field(default_factory=tuple)

    @property
    def relevant_ids(self) -> set[str]:
        return {memory_id for memory_id, gain in self.gains.items() if gain > 0}


def _binary_hits(ranked_ids: Sequence[str], relevant: set[str], k: int) -> int:
    return len(set(ranked_ids[:k]) & relevant)


def recall_at_k(ranked_ids: Sequence[str], relevant: Iterable[str], k: int) -> float:
    target = set(relevant)
    if not target:
        return 0.0
    return _binary_hits(ranked_ids, target, k) / len(target)


def precision_at_k(ranked_ids: Sequence[str], relevant: Iterable[str], k: int) -> float:
    if k <= 0:
        raise ValueError("k 必须大于 0")
    return _binary_hits(ranked_ids, set(relevant), k) / k


def hit_at_k(ranked_ids: Sequence[str], relevant: Iterable[str], k: int) -> float:
    return 1.0 if _binary_hits(ranked_ids, set(relevant), k) > 0 else 0.0


def mrr_at_k(ranked_ids: Sequence[str], relevant: Iterable[str], k: int) -> float:
    target = set(relevant)
    for index, memory_id in enumerate(ranked_ids[:k]):
        if memory_id in target:
            return 1.0 / (index + 1)
    return 0.0


def average_precision_at_k(ranked_ids: Sequence[str], relevant: Iterable[str], k: int) -> float:
    target = set(relevant)
    if not target:
        return 0.0
    hits = 0
    total = 0.0
    for index, memory_id in enumerate(ranked_ids[:k]):
        if memory_id in target:
            hits += 1
            total += hits / (index + 1)
    return total / min(len(target), k)


def dcg_at_k(ranked_ids: Sequence[str], gains: dict[str, int], k: int) -> float:
    total = 0.0
    for index, memory_id in enumerate(ranked_ids[:k]):
        gain = gains.get(memory_id, 0)
        if gain > 0:
            total += (2**gain - 1) / math.log2(index + 2)
    return total


def ndcg_at_k(ranked_ids: Sequence[str], gains: dict[str, int], k: int) -> float:
    ideal_gains = sorted((gain for gain in gains.values() if gain > 0), reverse=True)[:k]
    ideal = sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(ideal_gains))
    if ideal == 0:
        return 0.0
    return dcg_at_k(ranked_ids, gains, k) / ideal


def evaluate_ranked(
    ranked_ids: Sequence[str],
    gains: dict[str, int],
    *,
    k: int = PRIMARY_K,
) -> dict[str, float]:
    relevant = {memory_id for memory_id, gain in gains.items() if gain > 0}
    return {
        "recall": recall_at_k(ranked_ids, relevant, k),
        "precision": precision_at_k(ranked_ids, relevant, k),
        "hit": hit_at_k(ranked_ids, relevant, k),
        "mrr": mrr_at_k(ranked_ids, relevant, k),
        "map": average_precision_at_k(ranked_ids, relevant, k),
        "ndcg": ndcg_at_k(ranked_ids, gains, k),
    }


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def bootstrap_ci(
    values: Sequence[float],
    *,
    rounds: int = 1000,
    alpha: float = 0.05,
    seed: int = 20260415,
) -> tuple[float, float]:
    """对小型评估集给出均值的 bootstrap 百分位置信区间。"""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(_mean(sample))
    means.sort()
    lower = means[int(rounds * (alpha / 2))]
    upper = means[min(rounds - 1, int(rounds * (1 - alpha / 2)))]
    return lower, upper


def summarize(
    cases: Sequence[RankedCase],
    *,
    k: int = PRIMARY_K,
    with_ci: bool = True,
) -> dict[str, Any]:
    """整体与切片指标；切片取值为 ``case.slices`` 中的标签。"""
    return _summarize_subset(list(cases), k=k, with_ci=with_ci)


def summarize_all_ks(
    cases: Sequence[RankedCase],
    *,
    ks: Sequence[int] = REPORT_KS,
    with_ci: bool = True,
) -> dict[str, Any]:
    return {f"@{k}": summarize(cases, k=k, with_ci=with_ci) for k in ks}


def summarize_slices(
    cases: Sequence[RankedCase],
    *,
    k: int = PRIMARY_K,
    slices: Sequence[str] = ("time_sensitive", "cold_start", "long_sequence"),
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name in slices:
        subset = [case for case in cases if name in case.slices]
        if subset:
            report[name] = _summarize_subset(subset, k=k, with_ci=True)
    return report


def _summarize_subset(cases: list[RankedCase], *, k: int, with_ci: bool) -> dict[str, Any]:
    if not cases:
        return {"cases": 0}
    per_case = [evaluate_ranked(case.ranked_ids, case.gains, k=k) for case in cases]
    report: dict[str, Any] = {"cases": len(cases), "k": k}
    for name in ("recall", "precision", "hit", "mrr", "map", "ndcg"):
        values = [row[name] for row in per_case]
        report[name] = _mean(values)
        if with_ci:
            low, high = bootstrap_ci(values)
            report[f"{name}_ci95"] = [low, high]
    return report


def delta(candidate: dict[str, Any], baseline: dict[str, Any], metric: str) -> float:
    return float(candidate.get("all", candidate).get(metric, 0.0)) - float(
        baseline.get("all", baseline).get(metric, 0.0)
    )
