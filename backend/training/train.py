"""InfoNCE 训练脚本：训练投影层 + LSTM 事件编码器并导出模型包。

用法示例：
    python -m training.train --dataset artifacts/training.json --output-dir artifacts/event-model

训练数据需要包含 events/queries；若记录未带 base_vector，则读取项目 Settings
中的 Embedding Provider 现场计算，并写入 `--vector-cache` 指定的 JSONL 缓存。
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import torch
from torch.nn import functional as F

from training.dataset import (
    TrainingDataset,
    TrainingPair,
    build_training_pairs,
    build_windows,
    load_dataset,
    split_dataset,
)
from training.evaluate import evaluate_split
from training.manifest import (
    build_manifest,
    git_code_version,
    sha256_file,
    verify_package,
    write_package,
)
from training.model import EncoderModule, encode_queries, encode_windows, pad_windows
from training.vectors import TextVectorizer, resolve_vectors

DEFAULT_EPOCHS = 20
DEFAULT_BATCH_SIZE = 64
DEFAULT_TEMPERATURE = 0.07
DEFAULT_LEARNING_RATE = 1e-3
DEFAULT_WINDOW_SIZE = 8


@dataclass(frozen=True)
class TrainingConfig:
    epochs: int = DEFAULT_EPOCHS
    batch_size: int = DEFAULT_BATCH_SIZE
    temperature: float = DEFAULT_TEMPERATURE
    learning_rate: float = DEFAULT_LEARNING_RATE
    weight_decay: float = 0.01
    patience: int = 3
    window_size: int = DEFAULT_WINDOW_SIZE
    seed: int = 13


def _encode_events(
    module: EncoderModule,
    event_ids: Sequence[UUID],
    windows: dict[UUID, tuple[UUID, ...]],
    event_vectors: dict[UUID, list[float]],
    *,
    device: torch.device,
) -> torch.Tensor:
    padded = [
        [event_vectors[vector_id] for vector_id in windows[event_id]] for event_id in event_ids
    ]
    base, mask = pad_windows(padded, device=device)
    return encode_windows(module, base, mask)


def _hard_negatives(
    pairs: Sequence[TrainingPair], windows: dict[UUID, tuple[UUID, ...]]
) -> list[UUID]:
    seen: dict[str, UUID] = {}
    for pair in pairs:
        for event_id in pair.negative_event_ids:
            if event_id in windows:
                seen[str(event_id)] = event_id
    return [seen[key] for key in sorted(seen)][: len(pairs)]


def _pair_loss(
    module: EncoderModule,
    pairs: Sequence[TrainingPair],
    *,
    windows: dict[UUID, tuple[UUID, ...]],
    event_vectors: dict[UUID, list[float]],
    query_vectors: dict[UUID, list[float]],
    config: TrainingConfig,
    device: torch.device,
) -> torch.Tensor:
    query_ids = [pair.query_id for pair in pairs]
    positive_ids = [pair.positive_event_id for pair in pairs]
    negative_ids = _hard_negatives(pairs, windows)

    query_base = torch.tensor(
        [query_vectors[query_id] for query_id in query_ids], dtype=torch.float32, device=device
    )
    query_matrix = encode_queries(module, query_base)
    event_matrix = _encode_events(
        module, [*positive_ids, *negative_ids], windows, event_vectors, device=device
    )
    logits = query_matrix @ event_matrix.T / config.temperature
    labels = torch.arange(len(pairs), device=device)
    return F.cross_entropy(logits, labels)


def _batches(pairs: Sequence[TrainingPair], batch_size: int, rng: random.Random):
    indices = list(range(len(pairs)))
    rng.shuffle(indices)
    for start in range(0, len(indices), batch_size):
        yield [pairs[index] for index in indices[start : start + batch_size]]


def _batch_loss(
    module: EncoderModule,
    pairs: Sequence[TrainingPair],
    *,
    windows: dict[UUID, tuple[UUID, ...]],
    event_vectors: dict[UUID, list[float]],
    query_vectors: dict[UUID, list[float]],
    config: TrainingConfig,
    device: torch.device,
    optimizer: torch.optim.Optimizer | None = None,
) -> float:
    if optimizer is not None:
        module.train()
        optimizer.zero_grad(set_to_none=True)
        loss = _pair_loss(
            module,
            pairs,
            windows=windows,
            event_vectors=event_vectors,
            query_vectors=query_vectors,
            config=config,
            device=device,
        )
        loss.backward()
        torch.nn.utils.clip_grad_norm_(module.parameters(), 1.0)
        optimizer.step()
        return float(loss.detach().cpu())
    module.eval()
    with torch.no_grad():
        loss = _pair_loss(
            module,
            pairs,
            windows=windows,
            event_vectors=event_vectors,
            query_vectors=query_vectors,
            config=config,
            device=device,
        )
    return float(loss.detach().cpu())


def _mean_loss(
    module: EncoderModule,
    pairs: Sequence[TrainingPair],
    *,
    windows: dict[UUID, tuple[UUID, ...]],
    event_vectors: dict[UUID, list[float]],
    query_vectors: dict[UUID, list[float]],
    config: TrainingConfig,
    device: torch.device,
) -> float:
    if not pairs:
        return float("inf")
    losses = [
        _batch_loss(
            module,
            batch,
            windows=windows,
            event_vectors=event_vectors,
            query_vectors=query_vectors,
            config=config,
            device=device,
        )
        for batch in _batches(pairs, config.batch_size, random.Random(0))
    ]
    return sum(losses) / len(losses)


def train(
    *,
    dataset: TrainingDataset,
    output_dir: Path,
    model_version: str,
    config: TrainingConfig,
    device: str,
    base_embedding_dim: int,
    input_dim: int,
    hidden_dim: int,
    num_layers: int,
    embedding_model_version: str | None,
    dataset_version: str,
    code_version: str,
    vectorizer: TextVectorizer,
    split_kwargs: dict[str, float] | None = None,
) -> dict:
    """训练并导出模型包，返回包含指标与清单的摘要。"""
    split = split_dataset(dataset, **(split_kwargs or {}))
    if not split.train.events:
        raise ValueError("训练集为空，无法训练")
    pairs = build_training_pairs(split.train)
    if not pairs:
        raise ValueError("训练集没有查询—正例对，无法训练")

    torch_device = torch.device(device)
    torch.manual_seed(config.seed)
    random.seed(config.seed)

    windows = build_windows(split.train, window_size=config.window_size)
    event_vectors, query_vectors = resolve_vectors(split.train, vectorizer)
    validation_vectors = resolve_vectors(split.validation, vectorizer)
    validation_pairs = build_training_pairs(split.validation)
    validation_windows = build_windows(split.validation, window_size=config.window_size)

    module = EncoderModule(
        base_dim=base_embedding_dim,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        num_layers=num_layers,
    ).to(torch_device)
    optimizer = torch.optim.AdamW(
        module.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )

    best_loss = float("inf")
    best_state = {key: value.detach().cpu().clone() for key, value in module.state_dict().items()}
    stale_epochs = 0
    history: list[dict[str, float]] = []
    for epoch in range(1, config.epochs + 1):
        train_losses = []
        for batch in _batches(pairs, config.batch_size, random.Random(config.seed + epoch)):
            train_losses.append(
                _batch_loss(
                    module,
                    batch,
                    windows=windows,
                    event_vectors=event_vectors,
                    query_vectors=query_vectors,
                    config=config,
                    device=torch_device,
                    optimizer=optimizer,
                )
            )
        train_loss = sum(train_losses) / len(train_losses)
        if validation_pairs and validation_vectors[0]:
            validation_loss = _mean_loss(
                module,
                validation_pairs,
                windows=validation_windows,
                event_vectors=validation_vectors[0],
                query_vectors=validation_vectors[1],
                config=config,
                device=torch_device,
            )
        else:
            validation_loss = _mean_loss(
                module,
                pairs,
                windows=windows,
                event_vectors=event_vectors,
                query_vectors=query_vectors,
                config=config,
                device=torch_device,
            )
        history.append(
            {"epoch": epoch, "train_loss": train_loss, "validation_loss": validation_loss}
        )
        if validation_loss < best_loss - 1e-6:
            best_loss = validation_loss
            best_state = {
                key: value.detach().cpu().clone() for key, value in module.state_dict().items()
            }
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= config.patience:
                break

    module.load_state_dict(best_state)
    module.eval()

    test_split = split.test if split.test.queries else split.validation
    test_name = "test" if split.test.queries else "validation"
    test_vectors = validation_vectors
    if test_name == "test":
        test_vectors = resolve_vectors(split.test, vectorizer)
    report = evaluate_split(
        module=module,
        dataset=test_split,
        event_vectors=test_vectors[0],
        query_vectors=test_vectors[1],
        window_size=config.window_size,
        split_name=test_name,
        device=device,
    )

    training_summary = {
        "epochs_planned": config.epochs,
        "epochs_run": len(history),
        "batch_size": config.batch_size,
        "temperature": config.temperature,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "patience": config.patience,
        "window_size": config.window_size,
        "seed": config.seed,
        "best_validation_loss": best_loss,
        "history": history,
        "train_pairs": len(pairs),
        "validation_pairs": len(validation_pairs),
    }
    metrics = report.to_dict()
    manifest = build_manifest(
        model_version=model_version,
        base_embedding_dim=base_embedding_dim,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        num_layers=num_layers,
        weights_sha256="",
        dataset_version=dataset_version,
        code_version=code_version,
        training=training_summary,
        metrics=metrics,
        embedding_model_version=embedding_model_version,
    )
    package_config = {
        "model_version": model_version,
        "base_embedding_dim": base_embedding_dim,
        "input_dim": input_dim,
        "hidden_dim": hidden_dim,
        "num_layers": num_layers,
        "window_size": config.window_size,
        "training": training_summary,
        "metrics": metrics,
    }
    write_package(
        output_dir,
        state_dict=module.state_dict(),
        manifest=manifest,
        config=package_config,
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "dataset.json").write_text(
        json.dumps(
            {
                "dataset_version": dataset_version,
                "split": {
                    "cutoff": split.cutoff.isoformat() if split.cutoff else None,
                    "train_entities": [str(value) for value in split.train_entities],
                    "validation_entities": [str(value) for value in split.validation_entities],
                    "test_entities": [str(value) for value in split.test_entities],
                },
                "counts": {
                    "train_events": len(split.train.events),
                    "validation_events": len(split.validation.events),
                    "test_events": len(split.test.events),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    verification = verify_package(output_dir, expected_version=model_version)
    return {
        "model_version": model_version,
        "output_dir": str(output_dir),
        "metrics": metrics,
        "gate_passed": report.passed,
        "gate_failures": list(report.failures),
        "manifest_verified": verification["ok"],
        "manifest_errors": verification["errors"],
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="训练事件编码器并导出模型包")
    parser.add_argument("--dataset", required=True, help="训练数据 JSON 路径")
    parser.add_argument("--output-dir", required=True, help="模型包输出目录")
    parser.add_argument("--model-version", default=None, help="模型版本，默认按时间生成")
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--learning-rate", type=float, default=DEFAULT_LEARNING_RATE)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)
    parser.add_argument("--cutoff-ratio", type=float, default=0.8)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--vector-cache", default=None, help="基础向量 JSONL 缓存路径")
    parser.add_argument("--dataset-version", default=None, help="数据版本，默认取数据文件 SHA-256")
    parser.add_argument("--code-version", default=None, help="代码版本，默认读取 git HEAD")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from app.core.config import Settings
    from training.vectors import build_embedding_provider

    args = _parse_args(argv)
    settings = Settings()
    dataset_path = Path(args.dataset)
    dataset = load_dataset(dataset_path)
    model_version = args.model_version or (
        "event-lstm-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    provider = build_embedding_provider(settings) if settings.embedding_provider else None
    vectorizer = TextVectorizer(provider=provider, cache_path=args.vector_cache)

    try:
        summary = train(
            dataset=dataset,
            output_dir=Path(args.output_dir),
            model_version=model_version,
            config=TrainingConfig(
                epochs=args.epochs,
                batch_size=args.batch_size,
                temperature=args.temperature,
                learning_rate=args.learning_rate,
                patience=args.patience,
                window_size=args.window_size,
                seed=args.seed,
            ),
            device=args.device,
            base_embedding_dim=settings.base_embedding_dim,
            input_dim=settings.lstm_input_dim,
            hidden_dim=settings.lstm_hidden_dim,
            num_layers=settings.lstm_num_layers,
            embedding_model_version=settings.embedding_model_version,
            dataset_version=args.dataset_version or sha256_file(dataset_path),
            code_version=args.code_version or git_code_version(),
            vectorizer=vectorizer,
            split_kwargs={
                "val_ratio": args.val_ratio,
                "test_ratio": args.test_ratio,
                "cutoff_ratio": args.cutoff_ratio,
            },
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["gate_passed"] and summary["manifest_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
