from __future__ import annotations

from uuid import uuid4

import app.services.retrieval_reinforcement as reinforcement
from app.core.config import Settings
from app.services.retrieval_reinforcement import (
    normalize_query_fingerprint,
    reinforce_retrieval_hits,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://user:pass@localhost:5432/db",
    )


def test_query_fingerprint_normalizes_case_and_whitespace() -> None:
    assert normalize_query_fingerprint(
        "  Project   Alpha  的阈值 "
    ) == normalize_query_fingerprint("project alpha 的阈值")
    assert normalize_query_fingerprint("alpha 阈值") != normalize_query_fingerprint("alpha 阈值 2")


def test_reinforce_without_hits_is_noop() -> None:
    assert reinforce_retrieval_hits(
        settings=_settings(), actor_id="alice", query="任意查询", hits=[]
    ) == 0


def test_reinforce_write_failure_is_swallowed(monkeypatch) -> None:
    def boom(_database_url: str):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(reinforcement, "get_session_factory", boom)

    assert reinforce_retrieval_hits(
        settings=_settings(),
        actor_id="alice",
        query="任意查询",
        hits=[("event", uuid4())],
    ) == 0
