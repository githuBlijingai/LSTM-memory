"""AG-07 评估统一入口。

用法（在 memory-system 根目录）：
  python -m evaluation.cli all        # 重建数据集 + 离线评估 + 报告（有 DATABASE_URL 时附在线指标）
  python -m evaluation.cli dataset    # 只重建数据集
  python -m evaluation.cli offline    # 只跑离线评估 + 报告
  python -m evaluation.cli online     # 只跑在线评估
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evaluation.dataset import DATASET_PATH, load_dataset, validate_dataset, write_dataset
from evaluation.online import run_online
from evaluation.report import REPORT_PATH, write_report
from evaluation.runner import BASELINE_PATH, DEFAULT_MODEL_DIR, run_evaluation


def _run_offline(*, embedding: str, model_dir: Path) -> dict:
    payload = load_dataset(DATASET_PATH)
    problems = validate_dataset(payload)
    if problems:
        raise RuntimeError("数据集校验失败：" + "; ".join(problems))
    result = run_evaluation(payload, embedding_name=embedding, model_dir=model_dir)
    report_path = write_report(result, payload)
    print(f"离线基线：{BASELINE_PATH}")
    print(f"评估报告：{report_path}")
    print(
        "核心指标："
        f"event nDCG@10={result['event']['full']['@10']['ndcg']:.3f}, "
        f"wiki nDCG@10={result['wiki']['full']['@10']['ndcg']:.3f}, "
        f"unified nDCG@10={result['unified']['live']['@10']['ndcg']:.3f}, "
        f"gate={result['gate']['status']}"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AG-07 检索质量评估")
    parser.add_argument(
        "action",
        nargs="?",
        default="all",
        choices=("all", "dataset", "offline", "online"),
        help="all=数据集+离线+报告；dataset=只生成数据；offline=离线+报告；online=在线口径",
    )
    parser.add_argument(
        "--embedding",
        choices=("hash", "real"),
        default="hash",
        help="hash=确定性词法代理（默认）；real=运行期 EMBEDDING_PROVIDER",
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    if args.action in {"all", "dataset"}:
        path = write_dataset(DATASET_PATH)
        print(f"评估数据集：{path}")

    if args.action in {"all", "offline"}:
        result = _run_offline(embedding=args.embedding, model_dir=args.model_dir)
        if args.action == "all" and (args.database_url or os.getenv("DATABASE_URL")):
            try:
                online = run_online(database_url=args.database_url, days=args.days)
            except Exception as exc:  # 在线指标不阻断离线交付
                print(f"在线评估跳过：{exc}", file=sys.stderr)
            else:
                payload = load_dataset(DATASET_PATH)
                write_report(result, payload, online_result=online)
                print(f"在线指标：{json.dumps(online, ensure_ascii=False)}")

    if args.action == "online":
        online = run_online(database_url=args.database_url, days=args.days)
        print(json.dumps(online, ensure_ascii=False, indent=2))
        print(f"在线口径说明见 {REPORT_PATH.parent / 'REPORT.md'} 第 9 节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
