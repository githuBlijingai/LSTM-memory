from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from training.dataset import (
    DatasetError,
    EventRecord,
    QueryRecord,
    TrainingDataset,
    build_windows,
    parse_dataset,
    split_dataset,
)
from training.manifest import build_manifest, verify_package, write_package
from training.metrics import (
    RankedCase,
    evaluate_cases,
    gate_failures,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)
from training.model import EncoderModule
from training.train import TrainingConfig, train
from training.vectors import TextVectorizer

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _vector(rng: random.Random, dimension: int = 6) -> tuple[float, ...]:
    return tuple(rng.uniform(-1, 1) for _ in range(dimension))


def _synthetic_dataset(*, entity_count: int = 8, events_per_entity: int = 4) -> TrainingDataset:
    rng = random.Random(7)
    events: dict = {}
    queries: list[QueryRecord] = []
    for entity_index in range(entity_count):
        entity_id = uuid4()
        event_ids = []
        for item_index in range(events_per_entity):
            event_id = uuid4()
            event_ids.append(event_id)
            events[event_id] = EventRecord(
                event_id=event_id,
                entity_id=entity_id,
                text=f"实体{entity_index}事件{item_index}",
                event_time=BASE_TIME + timedelta(hours=item_index),
                sequence_no=item_index,
                base_vector=_vector(rng),
            )
        queries.append(
            QueryRecord(
                query_id=uuid4(),
                text=f"实体{entity_index}的查询",
                entity_id=entity_id,
                relevant_event_ids=(event_ids[0], event_ids[-1]),
                created_at=BASE_TIME + timedelta(hours=1),
                base_vector=_vector(rng),
            )
        )
    return TrainingDataset(events=events, queries=tuple(queries))


def test_parse_dataset_rejects_naive_datetime() -> None:
    with pytest.raises(DatasetError):
        parse_dataset(
            {
                "events": [
                    {
                        "event_id": str(uuid4()),
                        "entity_id": str(uuid4()),
                        "text": "事件",
                        "event_time": "2026-01-01T00:00:00",
                    }
                ],
                "queries": [],
            }
        )


def test_parse_dataset_rejects_duplicate_event_ids() -> None:
    event_id = str(uuid4())
    entity_id = str(uuid4())
    with pytest.raises(DatasetError):
        parse_dataset(
            {
                "events": [
                    {
                        "event_id": event_id,
                        "entity_id": entity_id,
                        "text": "A",
                        "event_time": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "event_id": event_id,
                        "entity_id": entity_id,
                        "text": "B",
                        "event_time": "2026-01-02T00:00:00+00:00",
                    },
                ],
                "queries": [],
            }
        )


def test_deleted_events_are_excluded_from_training() -> None:
    dataset = _synthetic_dataset(entity_count=2, events_per_entity=2)
    entity_id = next(iter(dataset.events.values())).entity_id
    deleted = EventRecord(
        event_id=uuid4(),
        entity_id=entity_id,
        text="已删除事件",
        event_time=BASE_TIME,
        deleted=True,
    )
    erased = EventRecord(
        event_id=uuid4(),
        entity_id=entity_id,
        text="隐私擦除事件",
        event_time=BASE_TIME,
        privacy_erased=True,
    )
    merged = TrainingDataset(
        events={**dataset.events, deleted.event_id: deleted, erased.event_id: erased},
        queries=dataset.queries,
    )

    usable_ids = set(merged.usable_events())

    assert deleted.event_id not in usable_ids
    assert erased.event_id not in usable_ids


def test_split_is_entity_disjoint_and_time_truncated() -> None:
    dataset = _synthetic_dataset(entity_count=9, events_per_entity=4)

    split = split_dataset(dataset, val_ratio=0.2, test_ratio=0.2, cutoff_ratio=0.5, seed=3)

    train_entities = set(split.train_entities)
    validation_entities = set(split.validation_entities)
    test_entities = set(split.test_entities)
    assert train_entities and validation_entities and test_entities
    assert not train_entities & validation_entities
    assert not train_entities & test_entities
    assert not validation_entities & test_entities
    assert split.cutoff is not None
    assert all(event.event_time <= split.cutoff for event in split.train.events.values())
    assert all(event.event_time > split.cutoff for event in split.validation.events.values())
    assert all(event.event_time > split.cutoff for event in split.test.events.values())
    for part in (split.train, split.validation, split.test):
        for query in part.queries:
            assert any(event_id in part.events for event_id in query.relevant_event_ids)


def test_build_windows_respects_size_and_order() -> None:
    dataset = _synthetic_dataset(entity_count=1, events_per_entity=5)

    windows = build_windows(dataset, window_size=3)

    assert len(windows) == 5
    for event_id, window in windows.items():
        assert window[-1] == event_id
        assert len(window) <= 3
    longest = max(windows.values(), key=len)
    times = [dataset.events[event_id].event_time for event_id in longest]
    assert times == sorted(times)


def test_metrics_known_values() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()

    assert recall_at_k([a, b, c], {c}, 10) == 1.0
    assert mrr_at_k([a, b, c], {c}, 10) == pytest.approx(1 / 3)
    assert ndcg_at_k([a, b, c], {c}, 10) == pytest.approx(0.5)
    assert recall_at_k([a, b], {c}, 10) == 0.0
    assert ndcg_at_k([a, b, c], {a, c}, 10) == pytest.approx(1.5 / 1.6309297535714575)


def test_metrics_report_groups_and_gate() -> None:
    a, b = uuid4(), uuid4()
    cases = [
        RankedCase(ranked_ids=(a, b), relevant_ids=frozenset({a}), time_sensitive=True),
        RankedCase(ranked_ids=(b, a), relevant_ids=frozenset({a})),
    ]

    report = evaluate_cases(cases, k=10)

    assert report["all"]["cases"] == 2
    assert report["all"]["recall"] == 1.0
    assert report["time_sensitive"]["cases"] == 1
    assert "cold_start" not in report

    baseline = {"all": {"recall": 1.0, "mrr": 0.75, "ndcg": 0.8}}
    assert gate_failures({"all": {"recall": 0.9, "mrr": 0.75, "ndcg": 0.8}}, baseline)
    assert not gate_failures({"all": {"recall": 1.0, "mrr": 0.75, "ndcg": 0.8}}, baseline)


def test_manifest_roundtrip_and_tamper_detection(tmp_path: Path) -> None:
    module = EncoderModule(base_dim=6, input_dim=4, hidden_dim=4, num_layers=1)
    manifest = build_manifest(
        model_version="event-lstm-test",
        base_embedding_dim=6,
        input_dim=4,
        hidden_dim=4,
        num_layers=1,
        weights_sha256="",
        dataset_version="dataset-sha",
        code_version="code-sha",
        training={"epochs_run": 1},
        metrics={"all": {"recall": 1.0}},
    )

    package_dir = write_package(
        tmp_path / "model",
        state_dict=module.state_dict(),
        manifest=manifest,
        config={"model_version": "event-lstm-test"},
    )

    verified = verify_package(package_dir, expected_version="event-lstm-test")
    assert verified["ok"] is True
    assert verified["errors"] == []
    stored = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert stored["weights_sha256"] == verified["weights_sha256"]

    (package_dir / "weights.safetensors").write_bytes(b"tampered")
    assert verify_package(package_dir, expected_version="event-lstm-test")["ok"] is False


def test_training_smoke_export(tmp_path: Path) -> None:
    dataset = _synthetic_dataset(entity_count=6, events_per_entity=4)
    output_dir = tmp_path / "event-model"

    summary = train(
        dataset=dataset,
        output_dir=output_dir,
        model_version="event-lstm-smoke",
        config=TrainingConfig(
            epochs=2,
            batch_size=2,
            temperature=0.07,
            learning_rate=1e-2,
            patience=1,
            window_size=2,
            seed=5,
        ),
        device="cpu",
        base_embedding_dim=6,
        input_dim=4,
        hidden_dim=4,
        num_layers=1,
        embedding_model_version="bge-test",
        dataset_version="dataset-sha",
        code_version="code-sha",
        vectorizer=TextVectorizer(),
        split_kwargs={"val_ratio": 0.2, "test_ratio": 0.2, "cutoff_ratio": 0.5},
    )

    assert summary["manifest_verified"] is True
    assert (output_dir / "weights.safetensors").is_file()
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "config.json").is_file()
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert "candidate" in metrics and "baseline" in metrics
    assert "gate" in metrics
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["base_embedding_dim"] == 6
    assert manifest["training"]["epochs_run"] >= 1
