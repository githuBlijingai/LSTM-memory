"""离线评估执行器：把数据集跑过与运行期一致的排序纯函数。

评估对象
--------
1. 事件与实体抽取：意图分类准确率、实体精确匹配率、事件字段完整率。
2. 事件检索：候选召回、Recall/MRR/nDCG（整体与切片）。
3. Wiki 检索：候选召回与排序质量。
4. 统一搜索：意图权重、双路融合、同词条去重。
5. 反馈信号：熟悉度开关对排序的影响。
6. 消融：纯向量、加精排、加衰减/熟悉度的逐级对比。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from app.domain.intent import WEIGHTS, IntentDecision, classify_intent
from app.domain.retrieval import RankedItem, fuse_ranked
from app.domain.scoring import (
    event_base_decay,
    event_memory_weight,
    materialize_familiarity,
    relevance,
    wiki_memory_weight,
)
from evaluation.dataset import EVAL_CLOCK, load_dataset
from evaluation.embedding import HashingEmbedding, LexicalReranker, RealEmbedding, build_embedding
from evaluation.metrics import (
    PRIMARY_K,
    RankedCase,
    summarize,
    summarize_all_ks,
    summarize_slices,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "artifacts" / "event-model"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
BASELINE_PATH = RESULTS_DIR / "baseline.json"

CANDIDATE_K = 100
TOP_N = 10
# 运行期阈值适用于 BGE 余弦尺度；词法代理的余弦分布整体偏低，直接套用会把
# 大量相关项挡在候选外。因此代理基线使用校准阈值 0.05，并单独报告生产阈值 0.15
# 下的候选召回，避免把"代理尺度差异"误判为系统召回缺陷。
PRODUCTION_MIN_SIMILARITY = 0.15
HASH_MIN_SIMILARITY = 0.05
MIN_SIMILARITY = HASH_MIN_SIMILARITY
DECAY_LAMBDA = 0.01
FAMILIARITY_GAMMA = 0.003
FAMILIARITY_MAX = 2.0
MEMORY_WEIGHT_CAP = 2.5
FAMILIARITY_BETA = 0.5
WIKI_FAMILIARITY_ALPHA = 0.6
PROTECT_HOURS = 48
MAX_WIKI_CHUNKS = 2

REFERENCE_TARGETS = {
    "event_candidate_recall": 0.95,
    "event_recall@10": 0.80,
    "event_ndcg@10": 0.70,
    "wiki_recall@10": 0.80,
    "wiki_ndcg@10": 0.70,
    "unified_ndcg@10": 0.70,
    "intent_auto_accuracy": 0.80,
}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _dot(left: list[float], right: list[float]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


@dataclass
class _QueryContext:
    query: dict[str, Any]
    vector: list[float]
    event_candidates: list[dict[str, Any]]
    wiki_candidates: list[dict[str, Any]]
    event_gains: dict[str, int]
    wiki_gains: dict[str, int]
    decision_live: Any
    decision_oracle: Any


class EvaluationRunner:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        embedding_name: str = "hash",
        clock: datetime = EVAL_CLOCK,
    ) -> None:
        self.payload = payload
        self.clock = clock
        self.embedding: HashingEmbedding | RealEmbedding = build_embedding(embedding_name)
        self.embedding_name = embedding_name
        self.min_similarity = (
            HASH_MIN_SIMILARITY if embedding_name == "hash" else PRODUCTION_MIN_SIMILARITY
        )
        self.events = payload["events"]
        self.chunks = [chunk for article in payload["wiki_articles"] for chunk in article["chunks"]]
        self.articles = {article["article_id"]: article for article in payload["wiki_articles"]}
        self.reranker = LexicalReranker()

        event_documents = [self._event_document(event) for event in self.events]
        wiki_documents = [chunk["raw_text"] for chunk in self.chunks]
        self.reranker.fit(event_documents + wiki_documents)
        self.event_vectors = dict(
            zip(
                (event["event_id"] for event in self.events),
                self.embedding.embed(event_documents),
                strict=True,
            )
        )
        self.wiki_vectors = dict(
            zip(
                (chunk["chunk_id"] for chunk in self.chunks),
                self.embedding.embed(wiki_documents),
                strict=True,
            )
        )
        self._contexts = [self._prepare(query) for query in payload["queries"]]

    # ---- 数据准备 ---------------------------------------------------------

    @staticmethod
    def _event_document(event: dict[str, Any]) -> str:
        return f"{event['title']}\n{event['content']}".strip()

    def _scored_events(
        self, query: dict[str, Any], *, rerank: bool, familiarity: bool
    ) -> list[dict[str, Any]]:
        context = next(item for item in self._contexts if item.query is query)
        weight_familiarity = MEMORY_WEIGHT_CAP
        rows = []
        for candidate in context.event_candidates:
            event = candidate["event"]
            decay = event_base_decay(
                event["initial_decay_score"],
                _parse_time(event["event_time"]),
                clock=lambda: self.clock,
                decay_lambda=DECAY_LAMBDA,
            )
            familiarity_now = materialize_familiarity(
                event["familiarity_score"],
                anchor=_parse_time(event["familiarity_updated_at"])
                if event.get("familiarity_updated_at")
                else None,
                created_at=_parse_time(event["event_time"]),
                clock=lambda: self.clock,
                gamma=FAMILIARITY_GAMMA,
                max_score=FAMILIARITY_MAX,
                protect_hours=PROTECT_HOURS,
            ).score
            ranking_familiarity = familiarity_now if familiarity else 0.0
            weight = event_memory_weight(
                decay,
                ranking_familiarity,
                cap=weight_familiarity,
                familiarity_beta=FAMILIARITY_BETA,
            )
            relevance_score = relevance(
                candidate["cosine"], candidate["rerank"] if rerank else None
            )
            rows.append(
                {
                    "memory_id": event["event_id"],
                    "record": event,
                    "cosine": candidate["cosine"],
                    "relevance": relevance_score,
                    "decay": decay,
                    "familiarity": familiarity_now,
                    "weight": weight,
                }
            )
        return rows

    def _scored_wiki(
        self, query: dict[str, Any], *, rerank: bool, familiarity: bool
    ) -> list[dict[str, Any]]:
        context = next(item for item in self._contexts if item.query is query)
        rows = []
        for candidate in context.wiki_candidates:
            chunk = candidate["chunk"]
            familiarity_now = materialize_familiarity(
                chunk["familiarity_score"],
                anchor=_parse_time(chunk["familiarity_updated_at"])
                if chunk.get("familiarity_updated_at")
                else None,
                created_at=self.clock,
                clock=lambda: self.clock,
                gamma=FAMILIARITY_GAMMA,
                max_score=FAMILIARITY_MAX,
                protect_hours=PROTECT_HOURS,
            ).score
            ranking_familiarity = familiarity_now if familiarity else 0.0
            weight = wiki_memory_weight(
                chunk["importance"],
                ranking_familiarity,
                cap=MEMORY_WEIGHT_CAP,
                wiki_familiarity_alpha=WIKI_FAMILIARITY_ALPHA,
            )
            relevance_score = relevance(
                candidate["cosine"], candidate["rerank"] if rerank else None
            )
            rows.append(
                {
                    "memory_id": chunk["chunk_id"],
                    "record": chunk,
                    "cosine": candidate["cosine"],
                    "relevance": relevance_score,
                    "decay": 1.0,
                    "familiarity": familiarity_now,
                    "weight": weight,
                }
            )
        return rows

    def _prepare(self, query: dict[str, Any]) -> _QueryContext:
        vector = self.embedding.embed([query["text"]])[0]
        event_candidates: list[dict[str, Any]] = []
        for event in self.events:
            if event["status"] != "active":
                continue
            cosine = _dot(vector, self.event_vectors[event["event_id"]])
            if cosine < self.min_similarity:
                continue
            event_candidates.append(
                {
                    "event": event,
                    "cosine": cosine,
                    "rerank": self.reranker.score(query["text"], self._event_document(event)),
                }
            )
        event_candidates.sort(key=lambda item: (-item["cosine"], item["event"]["event_id"]))
        event_candidates = event_candidates[:CANDIDATE_K]

        wiki_candidates: list[dict[str, Any]] = []
        for chunk in self.chunks:
            if chunk["status"] != "active":
                continue
            cosine = _dot(vector, self.wiki_vectors[chunk["chunk_id"]])
            if cosine < self.min_similarity:
                continue
            wiki_candidates.append(
                {
                    "chunk": chunk,
                    "cosine": cosine,
                    "rerank": self.reranker.score(query["text"], chunk["raw_text"]),
                }
            )
        wiki_candidates.sort(key=lambda item: (-item["cosine"], item["chunk"]["chunk_id"]))
        wiki_candidates = wiki_candidates[:CANDIDATE_K]

        decision_live = classify_intent(
            query["text"],
            memory_type=query["memory_type"],
            has_entity_filter=query["has_entity_filter"],
            has_time_filter=query["has_time_filter"],
        )
        decision_oracle = IntentDecision(
            query["expected_intent"],
            "评估用 oracle 意图（仅用于量化意图分类损失）",
            *WEIGHTS[query["expected_intent"]],
        )
        return _QueryContext(
            query=query,
            vector=vector,
            event_candidates=event_candidates,
            wiki_candidates=wiki_candidates,
            event_gains={label["memory_id"]: label["gain"] for label in query["event_labels"]},
            wiki_gains={label["memory_id"]: label["gain"] for label in query["wiki_labels"]},
            decision_live=decision_live,
            decision_oracle=decision_oracle,
        )

    # ---- 排序 -------------------------------------------------------------

    def _rank_events(
        self,
        query: dict[str, Any],
        *,
        rerank: bool,
        familiarity: bool,
        dense: bool = False,
    ) -> list[str]:
        rows = self._scored_events(query, rerank=rerank, familiarity=familiarity)
        if dense:
            rows.sort(key=lambda row: (-row["cosine"], row["memory_id"]))
        else:
            rows.sort(key=lambda row: (-(row["relevance"] * row["weight"]), row["memory_id"]))
        return [row["memory_id"] for row in rows[:TOP_N]]

    def _rank_wiki(
        self,
        query: dict[str, Any],
        *,
        rerank: bool,
        familiarity: bool,
        dense: bool = False,
    ) -> list[str]:
        rows = self._scored_wiki(query, rerank=rerank, familiarity=familiarity)
        if dense:
            rows.sort(key=lambda row: (-row["cosine"], row["memory_id"]))
        else:
            rows.sort(key=lambda row: (-(row["relevance"] * row["weight"]), row["memory_id"]))
        return [row["memory_id"] for row in rows[:TOP_N]]

    def _rank_unified(
        self,
        query: dict[str, Any],
        *,
        rerank: bool,
        familiarity: bool,
        intent_mode: str,
    ) -> tuple[list[str], int]:
        decision = (
            self._context_for(query).decision_live
            if intent_mode == "live"
            else self._context_for(query).decision_oracle
        )
        items: list[RankedItem] = []
        if query["memory_type"] in {"all", "event"}:
            for row in self._scored_events(query, rerank=rerank, familiarity=familiarity):
                items.append(
                    RankedItem(
                        memory_type="event",
                        memory_id=UUID(row["memory_id"]),
                        final_score=decision.event_weight * row["relevance"] * row["weight"],
                        tie_break=row["record"]["title"],
                    )
                )
        if query["memory_type"] in {"all", "wiki"}:
            for row in self._scored_wiki(query, rerank=rerank, familiarity=familiarity):
                chunk = row["record"]
                items.append(
                    RankedItem(
                        memory_type="wiki",
                        memory_id=UUID(row["memory_id"]),
                        final_score=decision.wiki_weight * row["relevance"] * row["weight"],
                        article_id=UUID(chunk["article_id"]),
                        tie_break=f"{chunk['article_id']}:{chunk['chunk_index']}",
                    )
                )
        kept = fuse_ranked(
            items,
            top_n=TOP_N,
            max_wiki_chunks_per_article=MAX_WIKI_CHUNKS,
        )
        per_article: dict[str, int] = {}
        for item in kept:
            if item.memory_type == "wiki" and item.article_id is not None:
                key = str(item.article_id)
                per_article[key] = per_article.get(key, 0) + 1
        violations = sum(1 for count in per_article.values() if count > MAX_WIKI_CHUNKS)
        return [str(item.memory_id) for item in kept], violations

    def _context_for(self, query: dict[str, Any]) -> _QueryContext:
        return next(item for item in self._contexts if item.query is query)


def _oracle_memory_type(intent: str) -> str:
    return {"event": "event", "wiki": "wiki", "mixed": "all"}.get(intent, "all")


def _cases(
    contexts: list[_QueryContext],
    ranker: Any,
    *,
    memory_kind: str,
) -> list[RankedCase]:
    cases: list[RankedCase] = []
    for context in contexts:
        query = context.query
        if memory_kind == "event":
            if query["memory_type"] not in {"all", "event"} or not context.event_gains:
                continue
            gains = context.event_gains
        elif memory_kind == "wiki":
            if query["memory_type"] not in {"all", "wiki"} or not context.wiki_gains:
                continue
            gains = context.wiki_gains
        else:
            gains = {**context.event_gains, **context.wiki_gains}
            if not gains:
                continue
        ranked = ranker(query)
        if isinstance(ranked, tuple):
            ranked = ranked[0]
        cases.append(
            RankedCase(
                query_key=query["key"],
                ranked_ids=tuple(ranked),
                gains=gains,
                slices=tuple(query["slices"]),
            )
        )
    return cases


def _candidate_recall(
    runner: EvaluationRunner,
    *,
    memory_kind: str,
    threshold: float,
) -> dict[str, Any]:
    total_relevant = 0
    covered = 0
    dropped: list[dict[str, str]] = []
    semantic_only: list[dict[str, str]] = []
    per_query: list[float] = []
    for context in runner._contexts:
        query = context.query
        if memory_kind == "event":
            if query["memory_type"] not in {"all", "event"} or not context.event_gains:
                continue
            relevant = {key for key, gain in context.event_gains.items() if gain > 0}
            candidates = {
                event["event_id"]
                for event in runner.events
                if event["status"] == "active"
                and _dot(context.vector, runner.event_vectors[event["event_id"]]) >= threshold
            }
        else:
            if query["memory_type"] not in {"all", "wiki"} or not context.wiki_gains:
                continue
            relevant = {key for key, gain in context.wiki_gains.items() if gain > 0}
            candidates = {
                chunk["chunk_id"]
                for chunk in runner.chunks
                if chunk["status"] == "active"
                and _dot(context.vector, runner.wiki_vectors[chunk["chunk_id"]]) >= threshold
            }
        if not relevant:
            continue
        hits = relevant & candidates
        total_relevant += len(relevant)
        covered += len(hits)
        per_query.append(len(hits) / len(relevant))
        for missing in sorted(relevant - candidates):
            dropped.append({"query": query["key"], "memory_id": missing})
            vector = (
                runner.event_vectors[missing]
                if memory_kind == "event"
                else runner.wiki_vectors[missing]
            )
            if _dot(context.vector, vector) <= 0.0:
                semantic_only.append({"query": query["key"], "memory_id": missing})
    return {
        "queries": len(per_query),
        "relevant_items": total_relevant,
        "covered_items": covered,
        "candidate_recall": (covered / total_relevant) if total_relevant else 0.0,
        "mean_query_recall": (sum(per_query) / len(per_query)) if per_query else 0.0,
        "dropped_relevant": dropped,
        "zero_overlap_relevant": semantic_only,
        "candidate_k": CANDIDATE_K,
        "min_similarity": threshold,
    }


def _intent_report(payload: dict[str, Any]) -> dict[str, Any]:
    confusion: dict[str, dict[str, int]] = {}
    failures: list[dict[str, str]] = []
    correct = 0
    auto_correct = 0
    auto_cases = 0
    for query in payload["queries"]:
        decision = classify_intent(
            query["text"],
            memory_type=query["memory_type"],
            has_entity_filter=query["has_entity_filter"],
            has_time_filter=query["has_time_filter"],
        )
        expected = query["expected_intent"]
        confusion.setdefault(expected, {})
        confusion[expected][decision.intent] = confusion[expected].get(decision.intent, 0) + 1
        is_correct = decision.intent == expected
        correct += int(is_correct)
        if query["memory_type"] == "all":
            auto_cases += 1
            auto_correct += int(is_correct)
            if not is_correct and len(failures) < 10:
                failures.append(
                    {
                        "query": query["key"],
                        "text": query["text"],
                        "expected": expected,
                        "predicted": decision.intent,
                        "reason": decision.reason,
                    }
                )
    total = len(payload["queries"])
    return {
        "cases": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "auto_cases": auto_cases,
        "auto_correct": auto_correct,
        "auto_accuracy": auto_correct / auto_cases if auto_cases else 0.0,
        "confusion": confusion,
        "failures": failures,
    }


def _entity_report(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload["events"]
    matched = [event for event in events if event["subject"] == event["canonical_subject"]]
    aliases = [
        {
            "event": event["key"],
            "subject": event["subject"],
            "canonical_subject": event["canonical_subject"],
        }
        for event in events
        if event["subject"] != event["canonical_subject"]
    ]
    subjects_by_entity: dict[str, set[str]] = {}
    for event in events:
        subjects_by_entity.setdefault(event["entity_key"], set()).add(event["subject"])
    fragmented = {
        key: sorted(subjects) for key, subjects in subjects_by_entity.items() if len(subjects) > 1
    }
    return {
        "events": len(events),
        "exact_match_events": len(matched),
        "exact_match_rate": len(matched) / len(events) if events else 0.0,
        "alias_events": aliases,
        "fragmented_entities": fragmented,
        "note": "运行期按 (owner_id, subject, entity_type) 精确匹配实体；别名会分裂为独立实体。",
    }


def _field_report(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload["events"]
    required = ("title", "content", "event_type", "event_time")
    complete = [
        event
        for event in events
        if all(str(event.get(field, "")).strip() for field in required)
        and len(event["title"]) <= 256
    ]
    return {
        "events": len(events),
        "complete_events": len(complete),
        "completeness": len(complete) / len(events) if events else 0.0,
        "required_fields": list(required),
    }


def _feedback_report(
    contexts: list[_QueryContext],
    runner: EvaluationRunner,
) -> dict[str, Any]:
    before_cases: list[RankedCase] = []
    after_cases: list[RankedCase] = []
    improved = 0
    degraded = 0
    rank_samples: list[dict[str, Any]] = []
    familiar_labels = 0
    for context in contexts:
        query = context.query
        gains = {**context.event_gains, **context.wiki_gains}
        if not gains:
            continue
        before = runner._rank_unified(
            query, rerank=True, familiarity=False, intent_mode="live"
        )[0]
        after = runner._rank_unified(
            query, rerank=True, familiarity=True, intent_mode="live"
        )[0]
        before_cases.append(
            RankedCase(query["key"], tuple(before), gains, tuple(query["slices"]))
        )
        after_cases.append(
            RankedCase(query["key"], tuple(after), gains, tuple(query["slices"]))
        )
        target = next(
            (
                label["memory_id"]
                for label in query["event_labels"] + query["wiki_labels"]
                if label["gain"] == 2
            ),
            None,
        )
        familiar_event_ids = {
            event["event_id"] for event in runner.events if event["familiarity_score"] > 0
        }
        familiar_chunk_ids = {
            chunk["chunk_id"] for chunk in runner.chunks if chunk["familiarity_score"] > 0
        }
        if target and (target in familiar_event_ids or target in familiar_chunk_ids):
            familiar_labels += 1
            before_rank = before.index(target) + 1 if target in before else None
            after_rank = after.index(target) + 1 if target in after else None
            if before_rank and after_rank:
                if after_rank < before_rank:
                    improved += 1
                elif after_rank > before_rank:
                    degraded += 1
            rank_samples.append(
                {
                    "query": query["key"],
                    "target": target,
                    "rank_before": before_rank,
                    "rank_after": after_rank,
                }
            )
    return {
        "familiar_queries": familiar_labels,
        "improved": improved,
        "degraded": degraded,
        "rank_samples": rank_samples,
        "without_familiarity": summarize(before_cases, k=PRIMARY_K),
        "with_familiarity": summarize(after_cases, k=PRIMARY_K),
        "delta_ndcg": (
            summarize(after_cases, k=PRIMARY_K)["ndcg"]
            - summarize(before_cases, k=PRIMARY_K)["ndcg"]
        ),
    }


def _gate(
    *,
    payload: dict[str, Any],
    embedding_name: str,
    model_dir: Path,
    event_candidate: dict[str, Any],
    event_primary: dict[str, Any],
    wiki_candidate: dict[str, Any],
    wiki_primary: dict[str, Any],
    unified_primary: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    if embedding_name != "real":
        blockers.append(
            "未配置真实 BGE Embedding，当前数字来自确定性词法代理，不能作为发布结论"
        )
    manifest = model_dir / "manifest.json"
    if not manifest.is_file():
        blockers.append(
            f"未找到事件模型包 {manifest}，无法执行设计 4.4 的“模型 vs 纯 BGE”发布门槛"
        )
    checks = {
        "event_candidate_recall": (
            event_candidate["candidate_recall"],
            REFERENCE_TARGETS["event_candidate_recall"],
        ),
        "event_recall@10": (event_primary["recall"], REFERENCE_TARGETS["event_recall@10"]),
        "event_ndcg@10": (event_primary["ndcg"], REFERENCE_TARGETS["event_ndcg@10"]),
        "wiki_recall@10": (wiki_primary["recall"], REFERENCE_TARGETS["wiki_recall@10"]),
        "wiki_ndcg@10": (wiki_primary["ndcg"], REFERENCE_TARGETS["wiki_ndcg@10"]),
        "unified_ndcg@10": (unified_primary["ndcg"], REFERENCE_TARGETS["unified_ndcg@10"]),
        "intent_auto_accuracy": (
            intent["auto_accuracy"],
            REFERENCE_TARGETS["intent_auto_accuracy"],
        ),
    }
    check_rows = {
        name: {
            "value": value,
            "target": target,
            "status": "pass" if value >= target else "fail",
        }
        for name, (value, target) in checks.items()
    }
    return {
        "status": "blocked" if blockers else (
            "fail" if any(row["status"] == "fail" for row in check_rows.values()) else "pass"
        ),
        "blockers": blockers,
        "checks": check_rows,
        "note": "参考线为本评估建议值；设计文档唯一硬门槛是测试集指标不得低于纯 BGE 基线。",
    }


def run_evaluation(
    payload: dict[str, Any] | None = None,
    *,
    embedding_name: str = "hash",
    model_dir: Path = DEFAULT_MODEL_DIR,
    output_path: Path = BASELINE_PATH,
) -> dict[str, Any]:
    payload = payload or load_dataset()
    runner = EvaluationRunner(payload, embedding_name=embedding_name)
    contexts = runner._contexts

    event_dense = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_events, rerank=False, familiarity=False, dense=True),
            memory_kind="event",
        )
    )
    event_rerank = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_events, rerank=True, familiarity=False),
            memory_kind="event",
        )
    )
    event_full = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_events, rerank=True, familiarity=True),
            memory_kind="event",
        )
    )
    wiki_dense = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_wiki, rerank=False, familiarity=False, dense=True),
            memory_kind="wiki",
        )
    )
    wiki_full = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_wiki, rerank=True, familiarity=True),
            memory_kind="wiki",
        )
    )

    dedupe_violations = 0

    def unified_live(query: dict[str, Any]) -> list[str]:
        nonlocal dedupe_violations
        ranked, violations = runner._rank_unified(
            query, rerank=True, familiarity=True, intent_mode="live"
        )
        dedupe_violations += violations
        return ranked

    unified_live_cases = _cases(contexts, unified_live, memory_kind="all")
    unified_live_report = summarize_all_ks(unified_live_cases)
    unified_live_slices = summarize_slices(unified_live_cases, k=PRIMARY_K)

    unified_oracle_full = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_unified, rerank=True, familiarity=True, intent_mode="oracle"),
            memory_kind="all",
        )
    )
    unified_no_rerank = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_unified, rerank=False, familiarity=True, intent_mode="live"),
            memory_kind="all",
        )
    )
    unified_no_familiarity = summarize_all_ks(
        _cases(
            contexts,
            partial(runner._rank_unified, rerank=True, familiarity=False, intent_mode="live"),
            memory_kind="all",
        )
    )

    event_candidate = _candidate_recall(
        runner, memory_kind="event", threshold=runner.min_similarity
    )
    event_candidate_production = _candidate_recall(
        runner, memory_kind="event", threshold=PRODUCTION_MIN_SIMILARITY
    )
    wiki_candidate = _candidate_recall(
        runner, memory_kind="wiki", threshold=runner.min_similarity
    )
    wiki_candidate_production = _candidate_recall(
        runner, memory_kind="wiki", threshold=PRODUCTION_MIN_SIMILARITY
    )
    intent = _intent_report(payload)
    entity = _entity_report(payload)
    fields = _field_report(payload)
    feedback = _feedback_report(contexts, runner)

    event_primary = event_full["@10"]
    wiki_primary = wiki_full["@10"]
    unified_primary = unified_live_report["@10"]
    gate = _gate(
        payload=payload,
        embedding_name=embedding_name,
        model_dir=model_dir,
        event_candidate=event_candidate,
        event_primary=event_primary,
        wiki_candidate=wiki_candidate,
        wiki_primary=wiki_primary,
        unified_primary=unified_primary,
        intent=intent,
    )

    result = {
        "metadata": {
            "dataset_version": payload["version"],
            "dataset_generated_at": payload["generated_at"],
            "eval_clock": payload["eval_clock"],
            "queries": len(payload["queries"]),
            "events": len(payload["events"]),
            "wiki_chunks": len(runner.chunks),
            "embedding": {
                "backend": embedding_name,
                "model_version": runner.embedding.model_version,
                "dimension": runner.embedding.dimension,
            },
            "reranker": runner.reranker.model_version,
            "candidate_k": CANDIDATE_K,
            "top_n": TOP_N,
            "min_similarity": runner.min_similarity,
            "production_min_similarity": PRODUCTION_MIN_SIMILARITY,
            "config": {
                "decay_lambda": DECAY_LAMBDA,
                "familiarity_gamma": FAMILIARITY_GAMMA,
                "memory_weight_cap": MEMORY_WEIGHT_CAP,
                "familiarity_beta": FAMILIARITY_BETA,
                "wiki_familiarity_alpha": WIKI_FAMILIARITY_ALPHA,
                "protect_hours": PROTECT_HOURS,
            },
        },
        "extraction": {
            "intent": intent,
            "entity_resolution": entity,
            "field_completeness": fields,
        },
        "event": {
            "candidate": event_candidate,
            "candidate_at_production_threshold": event_candidate_production,
            "dense": event_dense,
            "with_rerank": event_rerank,
            "full": event_full,
            "full_slices": summarize_slices(
                _cases(
                    contexts,
                    partial(runner._rank_events, rerank=True, familiarity=True),
                    memory_kind="event",
                ),
                k=PRIMARY_K,
            ),
        },
        "wiki": {
            "candidate": wiki_candidate,
            "candidate_at_production_threshold": wiki_candidate_production,
            "dense": wiki_dense,
            "full": wiki_full,
            "full_slices": summarize_slices(
                _cases(
                    contexts,
                    partial(runner._rank_wiki, rerank=True, familiarity=True),
                    memory_kind="wiki",
                ),
                k=PRIMARY_K,
            ),
        },
        "unified": {
            "live": unified_live_report,
            "live_slices": unified_live_slices,
            "oracle_intent": unified_oracle_full,
            "no_rerank": unified_no_rerank,
            "no_familiarity": unified_no_familiarity,
            "wiki_dedupe_violations": dedupe_violations,
            "intent_gain_ndcg": unified_live_report["@10"]["ndcg"]
            - unified_oracle_full["@10"]["ndcg"],
        },
        "feedback": feedback,
        "gate": gate,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def case_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "queries": len(payload["queries"]),
        "event_label_queries": sum(
            1
            for query in payload["queries"]
            if query["event_labels"] and query["memory_type"] in {"all", "event"}
        ),
        "wiki_label_queries": sum(
            1
            for query in payload["queries"]
            if query["wiki_labels"] and query["memory_type"] in {"all", "wiki"}
        ),
    }


if __name__ == "__main__":
    report = run_evaluation()
    print(json.dumps(report["metadata"], ensure_ascii=False, indent=2))
    print(f"基线结果：{BASELINE_PATH}")
