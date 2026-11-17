"""在线评估口径与脚本（可选，需要 PostgreSQL）。

离线基线回答“固定数据集上排序是否合理”；在线指标回答“真实流量里结果是否被使用”。
两者口径保持同一套相关性定义，便于对照：

- 曝光：memory_access_log 中 feedback_type='exposure' 的行，每个返回结果一行。
- 点击：feedback_type='clicked'；采纳：'adopted'；正反馈：'helpful'；负反馈：'not_helpful'。
- 反馈 nDCG@10：adopted=2，helpful/clicked=1，not_helpful=0，按曝光排名计算。
- 零结果率：窗口内没有任何曝光行的 memory_query 占比。
- 去重/额度：feedback_type<>'exposure' 的 not_counted_reason 分布。

延迟没有落库字段，因此本脚本不报告延迟；需要从 HTTP 指标另取。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from evaluation.metrics import ndcg_at_k

WINDOW_SQL = "q.created_at >= now() - make_interval(days => %(days)s)"
GAIN_BY_FEEDBACK = {
    "adopted": 2,
    "helpful": 1,
    "clicked": 1,
    "not_helpful": 0,
}


def _normalize_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


@dataclass
class OnlineRows:
    queries: list[dict[str, Any]]
    logs: list[dict[str, Any]]


def _load_rows(conn: Any, days: int) -> OnlineRows:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT q.query_id::text, q.intent, q.event_candidate_count, q.wiki_candidate_count,
                   EXISTS (
                       SELECT 1 FROM memory_access_log l WHERE l.query_id = q.query_id
                   ) AS has_results
            FROM memory_query q
            WHERE {WINDOW_SQL}
            """,
            {"days": days},
        )
        queries = [
            {
                "query_id": row[0],
                "intent": row[1],
                "event_candidates": row[2],
                "wiki_candidates": row[3],
                "has_results": row[4],
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            f"""
            SELECT l.query_id::text, l.memory_type::text, l.memory_id::text,
                   l.result_rank, l.feedback_type::text, l.is_counted,
                   l.not_counted_reason
            FROM memory_access_log l
            JOIN memory_query q ON q.query_id = l.query_id
            WHERE {WINDOW_SQL}
            """,
            {"days": days},
        )
        logs = [
            {
                "query_id": row[0],
                "memory_type": row[1],
                "memory_id": row[2],
                "rank": row[3],
                "feedback_type": row[4],
                "is_counted": row[5],
                "not_counted_reason": row[6],
            }
            for row in cur.fetchall()
        ]
    return OnlineRows(queries=queries, logs=logs)


def _impression_denominator(logs: list[dict[str, Any]], k: int | None) -> int:
    return sum(
        1
        for row in logs
        if row["feedback_type"] == "exposure" and (k is None or row["rank"] < k)
    )


def _rate(logs: list[dict[str, Any]], feedback_type: str, k: int | None) -> float:
    numerator = sum(
        1
        for row in logs
        if row["feedback_type"] == feedback_type and (k is None or row["rank"] < k)
    )
    denominator = _impression_denominator(logs, k)
    return numerator / denominator if denominator else 0.0


def _helpful_rate(logs: list[dict[str, Any]]) -> float:
    helped = sum(
        1
        for row in logs
        if row["feedback_type"] == "helpful" and row["is_counted"]
    )
    negative = sum(
        1
        for row in logs
        if row["feedback_type"] == "not_helpful" and row["is_counted"]
    )
    return helped / (helped + negative) if helped + negative else 0.0


def _feedback_ndcg(logs: list[dict[str, Any]], k: int = 10) -> dict[str, Any]:
    exposures: dict[str, list[tuple[int, str]]] = defaultdict(list)
    gains: dict[str, dict[str, int]] = defaultdict(dict)
    for row in logs:
        key = f"{row['memory_type']}:{row['memory_id']}"
        if row["feedback_type"] == "exposure":
            exposures[row["query_id"]].append((row["rank"], key))
        elif row["feedback_type"] in GAIN_BY_FEEDBACK:
            gain = GAIN_BY_FEEDBACK[row["feedback_type"]]
            current = gains[row["query_id"]].get(key)
            gains[row["query_id"]][key] = gain if current is None else max(current, gain)
    values = []
    for query_id, ranked in exposures.items():
        if not gains.get(query_id):
            continue
        ordered = [key for _, key in sorted(ranked)]
        values.append(ndcg_at_k(ordered, gains[query_id], k))
    return {
        "queries_with_feedback": len(values),
        "ndcg@10": sum(values) / len(values) if values else 0.0,
    }


def _percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return float(ordered[index])


def summarize_online(rows: OnlineRows, *, days: int) -> dict[str, Any]:
    queries = rows.queries
    logs = rows.logs
    intents: dict[str, int] = defaultdict(int)
    for query in queries:
        intents[query["intent"]] += 1
    reasons: dict[str, int] = defaultdict(int)
    for row in logs:
        if row["feedback_type"] != "exposure":
            reasons[row["not_counted_reason"] or "counted"] += 1
    event_candidates = [query["event_candidates"] for query in queries]
    wiki_candidates = [query["wiki_candidates"] for query in queries]
    zero_result = sum(1 for query in queries if not query["has_results"])
    return {
        "window_days": days,
        "queries": len(queries),
        "zero_result_queries": zero_result,
        "zero_result_rate": zero_result / len(queries) if queries else 0.0,
        "intent_distribution": dict(intents),
        "candidates": {
            "event_p50": _percentile(event_candidates, 0.5),
            "event_p95": _percentile(event_candidates, 0.95),
            "wiki_p50": _percentile(wiki_candidates, 0.5),
            "wiki_p95": _percentile(wiki_candidates, 0.95),
        },
        "ctr@10": _rate(logs, "clicked", 10),
        "adoption_rate@10": _rate(logs, "adopted", 10),
        "helpful_rate": _helpful_rate(logs),
        "feedback": _feedback_ndcg(logs, 10),
        "feedback_reason_distribution": dict(reasons),
        "impressions": _impression_denominator(logs, None),
        "note": "延迟未落库，需从 HTTP 指标获取；零结果率以窗口内无曝光行判定。",
    }


def run_online(*, database_url: str | None = None, days: int = 30) -> dict[str, Any]:
    dsn = database_url or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "未提供 DATABASE_URL，无法执行在线评估。请设置环境变量或使用 --database-url。"
        )
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise RuntimeError("缺少 psycopg，无法连接 PostgreSQL") from exc
    with psycopg.connect(_normalize_dsn(dsn)) as conn:
        rows = _load_rows(conn, days)
    return summarize_online(rows, days=days)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AG-07 在线评估（只读）")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    result = run_online(database_url=args.database_url, days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
