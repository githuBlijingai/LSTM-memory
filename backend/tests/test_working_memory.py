from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.models import WorkingItem
from app.models.enums import WorkingSessionStatus
from app.services.working_memory import (
    TOKEN_ESTIMATOR_VERSION,
    WorkingMemoryService,
    estimate_tokens,
    select_summary_items,
    should_roll_summary,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeScalars:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.items: list[WorkingItem] = []
        self.flush_count = 0

    def add(self, value: Any) -> None:
        self.added.append(value)
        if isinstance(value, WorkingItem):
            self.items.append(value)

    def scalar(self, statement: Any) -> int | None:
        return max((item.sequence_no for item in self.items), default=None)

    def scalars(self, statement: Any) -> FakeScalars:
        return FakeScalars(sorted(self.items, key=lambda item: item.sequence_no))

    def flush(self) -> None:
        self.flush_count += 1
        for item in self.items:
            if item.item_id is None:
                item.item_id = uuid4()


def _settings(**overrides: Any) -> Settings:
    return Settings(environment="test").model_copy(update=overrides)


def test_estimate_tokens_is_deterministic() -> None:
    assert TOKEN_ESTIMATOR_VERSION == "char-cjk-v1"
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("a你") == 2
    assert estimate_tokens("") == 0
    assert should_roll_summary(80, 100, 0.8)
    assert not should_roll_summary(79, 100, 0.8)


def test_working_memory_budget_and_summary_rollover() -> None:
    session = FakeSession()
    settings = _settings(
        working_memory_verbatim_items=2,
        working_memory_summary_ratio=0.8,
        working_memory_reserved_tokens=0,
    )
    service = WorkingMemoryService(session, settings, clock=lambda: NOW)  # type: ignore[arg-type]

    working_session = service.create_session("alice", token_budget=100)
    assert working_session.token_budget == 100
    assert working_session.token_count == 0
    assert working_session.status == WorkingSessionStatus.ACTIVE

    first = service.append_item(working_session, role="user", content="记" * 30)
    service.append_item(working_session, role="assistant", content="记" * 30)
    assert working_session.summary == ""

    service.append_item(working_session, role="user", content="记" * 30)
    assert working_session.token_count == 90
    assert working_session.summary_version == "rolling-summary-v1"
    assert "记" * 30 in working_session.summary
    assert first.consolidated_event_id is None


def test_latest_twenty_and_important_items_keep_verbatim() -> None:
    items = [
        WorkingItem(
            item_id=UUID(int=index),
            session_id=UUID(int=1000),
            sequence_no=index,
            role="user",
            content=f"item-{index}",
            token_count=1,
            importance=1.0 if index == 1 else 0.1,
            explicit_keep=index == 2,
        )
        for index in range(1, 26)
    ]
    verbatim, summarized = select_summary_items(items, keep_latest=20, important_threshold=0.7)
    verbatim_ids = {item.sequence_no for item in verbatim}
    summarized_ids = {item.sequence_no for item in summarized}

    assert set(range(6, 26)) <= verbatim_ids
    assert 1 in verbatim_ids
    assert 2 in verbatim_ids
    assert summarized_ids == {3, 4, 5}


def test_consolidation_links_items_and_closes_session() -> None:
    session = FakeSession()
    settings = _settings()
    drafts = []

    def writer(draft: Any) -> UUID:
        drafts.append(draft)
        return uuid4()

    service = WorkingMemoryService(
        session,
        settings,
        clock=lambda: NOW,
        event_writer=writer,  # type: ignore[arg-type]
    )
    working_session = service.create_session("alice")
    preference = service.append_item(
        working_session,
        role="user",
        content="我偏好使用中文回复",
        explicit_keep=True,
    )
    decision = service.append_item(
        working_session,
        role="assistant",
        content="决定采用方案 A",
        importance=0.8,
    )
    ignored = service.append_item(
        working_session,
        role="user",
        content="今天天气不错",
        importance=0.1,
    )

    result = service.consolidate(working_session)

    assert result.item_count == 2
    assert len(result.event_ids) == 2
    assert preference.consolidated_event_id == result.event_ids[0]
    assert decision.consolidated_event_id == result.event_ids[1]
    assert ignored.consolidated_event_id is None
    assert working_session.status == WorkingSessionStatus.CLOSED
    assert working_session.expires_at == NOW + timedelta(hours=24)
    assert [draft.event_type for draft in drafts] == ["explicit", "decision"]


def test_consolidation_merges_duplicate_content() -> None:
    session = FakeSession()
    settings = _settings()
    drafts = []

    def writer(draft: Any) -> UUID:
        drafts.append(draft)
        return uuid4()

    service = WorkingMemoryService(
        session,
        settings,
        clock=lambda: NOW,
        event_writer=writer,  # type: ignore[arg-type]
    )
    working_session = service.create_session("alice")
    first = service.append_item(
        working_session, role="user", content="偏好简洁回答", explicit_keep=True
    )
    second = service.append_item(
        working_session, role="user", content="偏好简洁回答", explicit_keep=True
    )

    result = service.consolidate(working_session)

    assert len(result.event_ids) == 1
    assert len(drafts) == 1
    assert drafts[0].item_ids == (first.item_id, second.item_id)
    assert first.consolidated_event_id == second.consolidated_event_id


def test_reserved_budget_must_be_smaller_than_budget() -> None:
    session = FakeSession()
    service = WorkingMemoryService(session, _settings(), clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(AppError) as error:
        service.create_session("alice", token_budget=2000)
    assert error.value.code == "INVALID_TOKEN_BUDGET"
