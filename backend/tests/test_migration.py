from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


def config_for(monkeypatch: pytest.MonkeyPatch) -> Config:
    backend = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://memory:memory@localhost:5432/memory")
    return Config(str(backend / "alembic.ini"))


def test_migrations_render_postgresql_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(monkeypatch)
    upgrade_output = io.StringIO()
    with redirect_stdout(upgrade_output):
        command.upgrade(config, "head", sql=True)
    upgrade_sql = upgrade_output.getvalue()

    assert (
        upgrade_sql.count("CREATE TABLE") - upgrade_sql.count("CREATE TABLE alembic_version") == 17
    )
    assert "CREATE EXTENSION IF NOT EXISTS vector" in upgrade_sql
    assert "CREATE TABLE retrieval_hit_gain" in upgrade_sql
    assert "CREATE TYPE visibility" in upgrade_sql
    assert "TIMESTAMP WITH TIME ZONE" in upgrade_sql
    assert "VECTOR(256)" in upgrade_sql
    assert "VECTOR(1024)" in upgrade_sql
    assert upgrade_sql.count("USING hnsw") == 2
    assert "source_event_key IS NOT NULL" in upgrade_sql
    assert "is_counted = true" in upgrade_sql
    assert "uq_retrieval_hit_gain_retrieval_hit_daily_once" in upgrade_sql
    assert "CURRENT_TIMESTAMP" not in upgrade_sql
    assert "ALTER TABLE entity ADD CONSTRAINT fk_entity_last_event_id_event_memory" in upgrade_sql

    hit_downgrade_output = io.StringIO()
    with redirect_stdout(hit_downgrade_output):
        command.downgrade(config, "3c9d2a7e5b11:74a69ff68f54", sql=True)
    hit_downgrade_sql = hit_downgrade_output.getvalue()
    assert "DROP INDEX ix_retrieval_hit_recent" in hit_downgrade_sql
    assert "DROP TABLE retrieval_hit_gain" in hit_downgrade_sql

    downgrade_output = io.StringIO()
    with redirect_stdout(downgrade_output):
        command.downgrade(config, "74a69ff68f54:base", sql=True)
    downgrade_sql = downgrade_output.getvalue()

    assert downgrade_sql.count("DROP TABLE") == 16
    assert (
        "ALTER TABLE entity DROP CONSTRAINT fk_entity_last_event_id_event_memory" in downgrade_sql
    )
    assert "DROP TYPE visibility" in downgrade_sql
    assert "DROP EXTENSION IF EXISTS vector" not in downgrade_sql
