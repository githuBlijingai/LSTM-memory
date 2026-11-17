from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB

from app.models import (
    AuditLog,
    Base,
    Entity,
    EventMemory,
    IdempotencyRecord,
    StateReplayLog,
    WikiChunk,
)

EXPECTED_TABLES = {
    "working_session",
    "working_item",
    "entity",
    "event_memory",
    "entity_state_snapshot",
    "state_replay_log",
    "wiki_article",
    "wiki_chunk",
    "wiki_sync_task",
    "memory_query",
    "memory_access_log",
    "familiarity_daily_gain",
    "outbox_event",
    "background_job",
    "idempotency_record",
    "audit_log",
}


def test_all_sixteen_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_datetime_columns_are_timezone_aware_and_json_fields_are_jsonb() -> None:
    datetime_columns = [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    ]

    assert datetime_columns
    assert all(column.type.timezone for column in datetime_columns)
    assert isinstance(Entity.__table__.c.summary_source_event_ids.type, JSONB)
    assert isinstance(AuditLog.__table__.c.before_json.type, JSONB)


def test_vector_dimensions_hnsw_and_partial_unique_indexes() -> None:
    event_vector = EventMemory.__table__.c.embedding.type
    wiki_vector = WikiChunk.__table__.c.embedding.type

    assert isinstance(event_vector, Vector) and event_vector.dim == 256
    assert isinstance(wiki_vector, Vector) and wiki_vector.dim == 1024
    event_indexes = {index.name: index for index in EventMemory.__table__.indexes}
    wiki_indexes = {index.name: index for index in WikiChunk.__table__.indexes}
    assert event_indexes["ix_event_embedding_hnsw"].kwargs["postgresql_using"] == "hnsw"
    assert wiki_indexes["ix_wiki_chunk_embedding_hnsw"].kwargs["postgresql_using"] == "hnsw"
    assert event_indexes["uq_event_source_event_key_active"].unique
    assert (
        event_indexes["uq_event_source_event_key_active"].dialect_options["postgresql"]["where"]
        is not None
    )
    replay_index = next(
        index
        for index in StateReplayLog.__table__.indexes
        if index.name == "uq_replay_event_target_active"
    )
    assert replay_index.unique


def test_idempotency_primary_key_matches_actor_key_path_contract() -> None:
    assert [column.name for column in IdempotencyRecord.__table__.primary_key.columns] == [
        "actor_id",
        "key",
        "request_path",
    ]


def test_audit_request_id_is_correlation_id_not_foreign_key() -> None:
    assert not AuditLog.__table__.c.request_id.foreign_keys
    assert EventMemory.__table__.c.entity_id.foreign_keys
