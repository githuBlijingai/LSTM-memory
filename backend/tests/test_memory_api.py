from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.api.v1 import entities, memory
from app.core.config import Settings
from app.core.security import require_admin
from app.domain.intent import classify_intent
from app.domain.retrieval import RankedItem, daily_gain, fuse_ranked, fuse_score
from app.main import create_app
from app.schemas.memory import EventCreateRequest, EventDeleteRequest, SearchRequest


def test_m1_routes_are_registered() -> None:
    spec = create_app(Settings(_env_file=None)).openapi()

    expected = {
        "/api/v1/memory/events",
        "/api/v1/memory/events/{event_id}",
        "/api/v1/memory/events/{event_id}/corrections",
        "/api/v1/memory/events/{event_id}/restore",
        "/api/v1/memory/search",
        "/api/v1/memory/feedback",
        "/api/v1/working/sessions",
        "/api/v1/working/sessions/{session_id}",
        "/api/v1/working/sessions/{session_id}/items",
        "/api/v1/working/sessions/{session_id}/consolidate",
        "/api/v1/entities",
        "/api/v1/entities/{entity_id}",
        "/api/v1/entities/{entity_id}/state",
        "/api/v1/entities/{entity_id}/events",
        "/api/v1/entities/{entity_id}/replays",
        "/api/v1/entities/{entity_id}/rebuild",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/retry",
        "/api/v1/dashboard/summary",
        "/api/v1/admin/memories/{memory_type}/{memory_id}/familiarity",
        "/api/v1/wiki/articles",
        "/api/v1/wiki/articles/{article_id}",
        "/api/v1/wiki/articles/{article_id}/chunks/{chunk_index}",
        "/api/v1/wiki/sync-tasks",
        "/api/v1/wiki/sync-tasks/{task_id}",
        "/api/v1/wiki/sync-tasks/{task_id}/retry",
    }
    assert expected <= set(spec["paths"])


def _header_names(operation: dict) -> set[str]:
    return {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "header"
    }


def test_write_operations_require_idempotency_key() -> None:
    spec = create_app(Settings(_env_file=None)).openapi()

    write_paths = [
        ("/api/v1/memory/events", "post"),
        ("/api/v1/memory/events/{event_id}/corrections", "post"),
        ("/api/v1/memory/events/{event_id}", "delete"),
        ("/api/v1/memory/events/{event_id}/restore", "post"),
        ("/api/v1/memory/feedback", "post"),
        ("/api/v1/working/sessions", "post"),
        ("/api/v1/working/sessions/{session_id}/items", "post"),
        ("/api/v1/working/sessions/{session_id}/consolidate", "post"),
        ("/api/v1/working/sessions/{session_id}", "delete"),
        ("/api/v1/entities/{entity_id}/rebuild", "post"),
        ("/api/v1/jobs/{job_id}/retry", "post"),
        ("/api/v1/wiki/sync-tasks", "post"),
        ("/api/v1/wiki/sync-tasks/{task_id}/retry", "post"),
    ]
    for path, method in write_paths:
        assert "Idempotency-Key" in _header_names(spec["paths"][path][method]), path
    assert "Idempotency-Key" not in _header_names(spec["paths"]["/api/v1/memory/search"]["post"])


def test_rebuild_requires_admin() -> None:
    """设计 8.2 / 契约 C-07：重建接口在路由层与服务层都要求 admin。"""
    from app.core.errors import AppError
    from app.core.security import CurrentUser, Role
    from app.services.memory_reads import MemoryReadService

    dependency = inspect.signature(entities.rebuild_entity).parameters["user"].default
    assert dependency.dependency is require_admin

    service = MemoryReadService(session=None, settings=Settings(_env_file=None))
    for role in (Role.OPERATOR, Role.VIEWER):
        user = CurrentUser(subject=role.value, roles=frozenset({role}))
        with pytest.raises(AppError) as denied:
            service.request_rebuild(user, UUID("00000000-0000-0000-0000-000000000001"))
        assert denied.value.status_code == 403


def test_event_mutations_require_admin() -> None:
    """设计 8.2：纠偏与归档/清除在路由层就要求 admin。"""
    for handler in (memory.correct_event, memory.delete_event):
        dependency = inspect.signature(handler).parameters["user"].default
        assert dependency.dependency is require_admin


def test_event_request_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(
            subject="alice",
            event_type="meeting",
            event_time="2026-01-01T00:00:00",
            title="会议",
            content="决定了发布计划",
        )


def test_search_request_validates_time_range() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(
            query="上次会议决定",
            filters={"start_time": "2026-02-01T00:00:00Z", "end_time": "2026-01-01T00:00:00Z"},
        )


def test_delete_request_defaults_to_archive() -> None:
    request = EventDeleteRequest(reason="误录入")

    assert request.mode == "archive"


def test_intent_rules_and_weights() -> None:
    assert classify_intent("上次我们决定的方案").intent == "event"
    assert classify_intent("向量数据库是什么").intent == "wiki"
    assert classify_intent("昨天聊了什么概念").intent in {"event", "wiki", "mixed"}
    assert classify_intent("随便问问").intent == "mixed"

    filtered = classify_intent("查询", has_entity_filter=True)
    assert filtered.intent == "event"
    assert filtered.event_weight == 0.8 and filtered.wiki_weight == 0.2

    only_wiki = classify_intent("上次会议", memory_type="wiki")
    assert only_wiki.intent == "wiki"
    assert only_wiki.event_weight == 0.0 and only_wiki.wiki_weight == 1.0


def test_fusion_sorts_and_caps_wiki_chunks() -> None:
    article_a = uuid4()
    article_b = uuid4()
    items = [
        RankedItem("wiki", uuid4(), 0.9, article_id=article_a, tie_break="a0"),
        RankedItem("wiki", uuid4(), 0.85, article_id=article_a, tie_break="a1"),
        RankedItem("wiki", uuid4(), 0.8, article_id=article_a, tie_break="a2"),
        RankedItem("wiki", uuid4(), 0.7, article_id=article_b, tie_break="b0"),
        RankedItem("event", uuid4(), 0.75),
    ]

    ranked = fuse_ranked(items, top_n=10)

    assert [item.final_score for item in ranked] == [0.9, 0.85, 0.75, 0.7]
    assert sum(1 for item in ranked if item.article_id == article_a) == 2


def test_fuse_score_is_bounded() -> None:
    assert fuse_score(0.8, 0.5, 0.5) == pytest.approx(0.2)
    assert fuse_score(2, -1, 3) == 0.0


def test_daily_gain_respects_remaining_cap() -> None:
    assert daily_gain(0.1, 0.08) == pytest.approx(0.08)
    assert daily_gain(0.02, 0.08) == pytest.approx(0.02)
    assert daily_gain(0.0, 0.08) == 0.0
