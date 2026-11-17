"""基线评估：训练后的 Query Adapter + LSTM 与纯 BGE 检索对比。

指标沿用设计 4.4 的发布门槛：Recall@10、MRR@10、nDCG@10 不得低于纯 BGE 基线。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import torch

from training.dataset import TrainingDataset, build_windows, load_dataset, split_dataset
from training.metrics import RankedCase, evaluate_cases, gate_failures
from training.model import EncoderModule, encode_queries, encode_windows, pad_windows

ENCODE_BATCH_SIZE = 64


@dataclass(frozen=True)
class EvaluationReport:
    split: str
    queries: int
    candidates: int
    candidate: dict[str, Any]
    baseline: dict[str, Any]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "split": self.split,
            "queries": self.queries,
            "candidates": self.candidates,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "gate": {"passed": self.passed, "failures": list(self.failures)},
        }


def _matrix(vectors: list[list[float]], device: torch.device) -> torch.Tensor:
    return torch.tensor(vectors, dtype=torch.float32, device=device)


def encode_event_matrix(
    module: EncoderModule,
    event_ids: list[UUID],
    windows: dict[UUID, tuple[UUID, ...]],
    event_vectors: dict[UUID, list[float]],
    *,
    device: torch.device,
) -> torch.Tensor:
    """按窗口编码全部候选事件，返回 [N, hidden] 的 L2 归一化矩阵。"""
    module.eval()
    outputs: list[torch.Tensor] = []
    with torch.no_grad():
        for start in range(0, len(event_ids), ENCODE_BATCH_SIZE):
            batch_ids = event_ids[start : start + ENCODE_BATCH_SIZE]
            padded = [
                [event_vectors[vector_id] for vector_id in windows[event_id]]
                for event_id in batch_ids
            ]
            base, mask = pad_windows(padded, device=device)
            outputs.append(encode_windows(module, base, mask))
    if not outputs:
        return torch.zeros(0, module.hidden_dim, device=device)
    return torch.cat(outputs, dim=0)


def rank_case_ids(scores: torch.Tensor, event_ids: list[UUID]) -> list[list[UUID]]:
    order = torch.argsort(scores, dim=1, descending=True, stable=True)
    return [[event_ids[index] for index in row.tolist()] for row in order]


def evaluate_split(
    *,
    module: EncoderModule,
    dataset: TrainingDataset,
    event_vectors: dict[UUID, list[float]],
    query_vectors: dict[UUID, list[float]],
    window_size: int = 8,
    split_name: str = "test",
    top_k: int = 10,
    device: str = "cpu",
) -> EvaluationReport:
    """在给定集合上同时评估训练模型与纯 BGE 基线。"""
    torch_device = torch.device(device)
    windows = build_windows(dataset, window_size=window_size)
    event_ids = sorted(windows, key=str)
    queries = [
        query
        for query in dataset.queries
        if any(event_id in windows for event_id in query.relevant_event_ids)
    ]
    if not queries or not event_ids:
        return EvaluationReport(
            split=split_name,
            queries=len(queries),
            candidates=len(event_ids),
            candidate={"k": top_k, "all": {"cases": 0}},
            baseline={"k": top_k, "all": {"cases": 0}},
            failures=(),
        )

    module.eval()
    with torch.no_grad():
        trained_scores = (
            encode_queries(
                module, _matrix([query_vectors[query.query_id] for query in queries], torch_device)
            )
            @ encode_event_matrix(module, event_ids, windows, event_vectors, device=torch_device).T
        )
        baseline_scores = (
            _matrix([query_vectors[query.query_id] for query in queries], torch_device)
            @ _matrix([event_vectors[event_id] for event_id in event_ids], torch_device).T
        )

    trained_cases: list[RankedCase] = []
    baseline_cases: list[RankedCase] = []
    for row, query in enumerate(queries):
        relevant = frozenset(
            event_id for event_id in query.relevant_event_ids if event_id in windows
        )
        if not relevant:
            continue
        trained_cases.append(
            RankedCase(
                ranked_ids=tuple(rank_case_ids(trained_scores[row : row + 1], event_ids)[0]),
                relevant_ids=relevant,
                time_sensitive=query.time_sensitive,
                cold_start=query.cold_start,
                long_sequence=query.long_sequence,
            )
        )
        baseline_cases.append(
            RankedCase(
                ranked_ids=tuple(rank_case_ids(baseline_scores[row : row + 1], event_ids)[0]),
                relevant_ids=relevant,
                time_sensitive=query.time_sensitive,
                cold_start=query.cold_start,
                long_sequence=query.long_sequence,
            )
        )

    candidate_report = evaluate_cases(trained_cases, k=top_k)
    baseline_report = evaluate_cases(baseline_cases, k=top_k)
    return EvaluationReport(
        split=split_name,
        queries=len(trained_cases),
        candidates=len(event_ids),
        candidate=candidate_report,
        baseline=baseline_report,
        failures=tuple(gate_failures(candidate_report, baseline_report)),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评估事件编码模型与纯 BGE 基线")
    parser.add_argument("--dataset", required=True, help="训练数据 JSON 路径")
    parser.add_argument("--model-dir", required=True, help="模型包目录")
    parser.add_argument("--split", default="test", choices=["train", "validation", "test"])
    parser.add_argument("--window-size", type=int, default=8)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--vector-cache", default=None, help="基础向量 JSONL 缓存路径")
    parser.add_argument("--output", default=None, help="评估结果 JSON 输出路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from app.core.config import Settings
    from app.ml.registry import load_model_package, load_model_weights
    from training.vectors import TextVectorizer, build_embedding_provider, resolve_vectors

    args = _parse_args(argv)
    settings = Settings()
    dataset = load_dataset(args.dataset)
    split = split_dataset(dataset)
    selected = {"train": split.train, "validation": split.validation, "test": split.test}[
        args.split
    ]

    package = load_model_package(
        args.model_dir,
        expected_version=_manifest_version(Path(args.model_dir)),
        base_embedding_dim=settings.base_embedding_dim,
        input_dim=settings.lstm_input_dim,
        hidden_dim=settings.lstm_hidden_dim,
        num_layers=settings.lstm_num_layers,
    )
    module = EncoderModule(
        base_dim=settings.base_embedding_dim,
        input_dim=settings.lstm_input_dim,
        hidden_dim=settings.lstm_hidden_dim,
        num_layers=settings.lstm_num_layers,
    )
    module.load_state_dict(load_model_weights(package), strict=True)

    provider = build_embedding_provider(settings) if settings.embedding_provider else None
    vectorizer = TextVectorizer(provider=provider, cache_path=args.vector_cache)
    event_vectors, query_vectors = resolve_vectors(selected, vectorizer)

    report = evaluate_split(
        module=module,
        dataset=selected,
        event_vectors=event_vectors,
        query_vectors=query_vectors,
        window_size=args.window_size,
        split_name=args.split,
        top_k=args.top_k,
        device=args.device,
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload)
    if not report.passed:
        print("发布门槛未通过：" + "; ".join(report.failures), file=sys.stderr)
        return 1
    return 0


def _manifest_version(model_dir: Path) -> str:
    manifest = json.loads((model_dir / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest.get("model_version") or "")


if __name__ == "__main__":
    raise SystemExit(main())
