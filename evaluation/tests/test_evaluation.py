from __future__ import annotations

import json
from pathlib import Path

import pytest
from evaluation import DATASET_VERSION
from evaluation.dataset import build_dataset, load_dataset, validate_dataset, write_dataset
from evaluation.embedding import HashingEmbedding, LexicalReranker
from evaluation.metrics import (
    RankedCase,
    average_precision_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
    summarize,
)
from evaluation.online import OnlineRows, summarize_online
from evaluation.runner import run_evaluation


def test_metrics_perfect_and_graded() -> None:
    gains = {"a": 2, "b": 1, "c": 0}
    assert recall_at_k(["a", "b"], {"a", "b"}, 10) == 1.0
    assert mrr_at_k(["x", "a"], {"a"}, 10) == 0.5
    assert average_precision_at_k(["a", "b"], {"a", "b"}, 10) == 1.0
    assert ndcg_at_k(["a", "b"], gains, 10) == pytest.approx(1.0)
    assert ndcg_at_k(["b", "a"], gains, 10) < 1.0
    assert ndcg_at_k(["c"], gains, 10) == 0.0


def test_summary_slices() -> None:
    cases = [
        RankedCase("q1", ("a", "b"), {"a": 2, "b": 1}, ("cold_start",)),
        RankedCase("q2", ("x", "b"), {"b": 1}, ("long_sequence",)),
    ]
    report = summarize(cases, k=10, with_ci=False)
    assert report["cases"] == 2
    assert report["ndcg"] == pytest.approx((1.0 + ndcg_at_k(["x", "b"], {"b": 1}, 10)) / 2)


def test_dataset_is_deterministic_and_valid(tmp_path: Path) -> None:
    first = write_dataset(tmp_path / "a.json")
    second = write_dataset(tmp_path / "b.json")
    assert first.read_bytes() == second.read_bytes()

    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["version"] == DATASET_VERSION
    assert validate_dataset(payload) == []
    assert len(payload["events"]) == 51
    assert len(payload["queries"]) == 34
    assert sum(len(article["chunks"]) for article in payload["wiki_articles"]) == 24

    event_ids = {event["event_id"] for event in payload["events"]}
    assert len(event_ids) == len(payload["events"])
    alias_events = [
        event for event in payload["events"] if event["subject"] != event["canonical_subject"]
    ]
    assert len(alias_events) == 1


def test_dataset_has_all_slices() -> None:
    payload = build_dataset().payload
    slices = {name for query in payload["queries"] for name in query["slices"]}
    assert {"time_sensitive", "cold_start", "long_sequence"} <= slices
    auto_queries = [query for query in payload["queries"] if query["memory_type"] == "all"]
    assert len(auto_queries) >= 20


def test_embedding_and_reranker_are_deterministic() -> None:
    embedder = HashingEmbedding()
    first = embedder.embed(["余弦相似度用于向量召回"])[0]
    second = embedder.embed(["余弦相似度用于向量召回"])[0]
    assert first == second
    related = embedder.embed(["余弦相似度衡量向量方向相似程度"])[0]
    unrelated = embedder.embed(["发布流水线 v12 增加评估报告"])[0]

    def dot(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert dot(first, related) > dot(first, unrelated)

    reranker = LexicalReranker(["余弦相似度衡量向量方向相似程度", "发布流水线增加评估报告"])
    assert reranker.score("余弦相似度", "余弦相似度衡量向量方向相似程度") > reranker.score(
        "余弦相似度", "发布流水线增加评估报告"
    )


def test_offline_evaluation_smoke(tmp_path: Path) -> None:
    result = run_evaluation(output_path=tmp_path / "baseline.json")
    assert result["event"]["full"]["@10"]["cases"] > 0
    assert result["wiki"]["full"]["@10"]["cases"] > 0
    assert result["unified"]["live"]["@10"]["cases"] == 34
    assert 0.0 <= result["unified"]["live"]["@10"]["ndcg"] <= 1.0
    assert result["unified"]["wiki_dedupe_violations"] == 0
    assert result["gate"]["status"] == "blocked"
    assert (tmp_path / "baseline.json").is_file()


def test_online_summary_from_rows() -> None:
    rows = OnlineRows(
        queries=[
            {"query_id": "q1", "intent": "mixed", "event_candidates": 10,
             "wiki_candidates": 0, "has_results": True},
            {"query_id": "q2", "intent": "wiki", "event_candidates": 0,
             "wiki_candidates": 0, "has_results": False},
        ],
        logs=[
            {"query_id": "q1", "memory_type": "event", "memory_id": "m1", "rank": 0,
             "feedback_type": "exposure", "is_counted": False, "not_counted_reason": None},
            {"query_id": "q1", "memory_type": "event", "memory_id": "m1", "rank": 0,
             "feedback_type": "adopted", "is_counted": True, "not_counted_reason": None},
            {"query_id": "q1", "memory_type": "event", "memory_id": "m2", "rank": 1,
             "feedback_type": "exposure", "is_counted": False, "not_counted_reason": None},
            {"query_id": "q1", "memory_type": "event", "memory_id": "m2", "rank": 1,
             "feedback_type": "clicked", "is_counted": False,
             "not_counted_reason": "duplicate_feedback"},
        ],
    )
    summary = summarize_online(rows, days=7)
    assert summary["queries"] == 2
    assert summary["zero_result_rate"] == pytest.approx(0.5)
    assert summary["adoption_rate@10"] == pytest.approx(0.5)
    assert summary["feedback"]["ndcg@10"] == pytest.approx(1.0)
    assert summary["feedback_reason_distribution"]["duplicate_feedback"] == 1
    assert summary["candidates"]["event_p50"] == 0.0


def test_checked_in_dataset_is_current() -> None:
    dataset_path = Path(__file__).resolve().parents[1] / "data" / "eval_dataset.json"
    assert dataset_path.is_file()
    assert validate_dataset(load_dataset(dataset_path)) == []
