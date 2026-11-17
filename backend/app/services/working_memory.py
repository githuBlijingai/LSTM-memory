from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import WorkingItem, WorkingSession
from app.models.enums import WorkingSessionStatus

TOKEN_ESTIMATOR_VERSION = "char-cjk-v1"
ROLLING_SUMMARY_VERSION = "rolling-summary-v1"
MAX_ROLLING_SUMMARY_CHARS_PER_ITEM = 120
NON_CJK_CHARS_PER_TOKEN = 4

CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
)

CONSOLIDATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("preference", ("偏好", "喜欢", "讨厌", "习惯", "prefer", "favorite")),
    ("decision", ("决定", "决策", "确定", "选择", "采用", "decided", "decision")),
    ("task_result", ("完成", "失败", "结果", "已修复", "done", "result")),
    ("fact", ("事实", "记录", "信息", "注意", "fact")),
)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """确定性 Token 估算：中日韩字符按 1，其他字符按 4 字符/token。"""
    if not text:
        return 0
    cjk_count = sum(1 for character in text if _is_cjk(character))
    other_count = len(text) - cjk_count
    return max(1, cjk_count + math.ceil(other_count / NON_CJK_CHARS_PER_TOKEN))


def should_roll_summary(token_count: int, token_budget: int, ratio: float) -> bool:
    if token_budget <= 0:
        raise ValueError("token_budget must be positive")
    if not 0 < ratio <= 1:
        raise ValueError("ratio must be within (0,1]")
    return token_count >= token_budget * ratio


def normalize_content(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def content_digest(text: str) -> str:
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()


def select_summary_items(
    items: Iterable[WorkingItem],
    *,
    keep_latest: int,
    important_threshold: float,
) -> tuple[list[WorkingItem], list[WorkingItem]]:
    """返回 (保留原文条目, 参与滚动摘要条目)。

    最新 N 条、重要条目与显式保留条目始终保留原文。
    """
    if keep_latest < 0:
        raise ValueError("keep_latest must be non-negative")
    ordered = sorted(items, key=lambda item: item.sequence_no)
    latest_ids = {item.item_id for item in ordered[-keep_latest:]} if keep_latest else set()
    verbatim: list[WorkingItem] = []
    summarized: list[WorkingItem] = []
    for item in ordered:
        if (
            item.item_id in latest_ids
            or item.explicit_keep
            or item.importance >= important_threshold
        ):
            verbatim.append(item)
        else:
            summarized.append(item)
    return verbatim, summarized


def compose_rolling_summary(items: Iterable[WorkingItem]) -> str:
    lines = []
    for item in sorted(items, key=lambda value: value.sequence_no):
        content = normalize_content(item.content)
        if len(content) > MAX_ROLLING_SUMMARY_CHARS_PER_ITEM:
            content = content[:MAX_ROLLING_SUMMARY_CHARS_PER_ITEM] + "…"
        lines.append(f"{item.role}: {content}")
    return "\n".join(lines)


def classify_consolidation(item: WorkingItem) -> str | None:
    """仅在命中事实、偏好、决策、任务结果或显式标记时返回事件类型。"""
    if item.explicit_keep:
        return "explicit"
    content = item.content.lower()
    for event_type, keywords in CONSOLIDATION_RULES:
        if any(keyword in content for keyword in keywords):
            return event_type
    return None


def is_consolidation_candidate(item: WorkingItem, *, important_threshold: float) -> bool:
    return (
        item.explicit_keep
        or item.importance >= important_threshold
        or classify_consolidation(item) is not None
    )


@dataclass(frozen=True)
class ConsolidationDraft:
    session_id: UUID
    item_ids: tuple[UUID, ...]
    role: str
    content: str
    event_type: str
    importance: float
    explicit_keep: bool
    content_hash: str


@dataclass(frozen=True)
class ConsolidationResult:
    event_ids: list[UUID]
    item_count: int
    drafts: list[ConsolidationDraft]


class EventWriter(Protocol):
    def __call__(self, draft: ConsolidationDraft) -> UUID:
        """把巩固草稿写入事件管道并返回事件 ID。"""


class WorkingMemoryService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        event_writer: EventWriter | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.clock = clock
        self.event_writer = event_writer

    def create_session(
        self,
        actor_id: str,
        *,
        session_id: UUID | None = None,
        token_budget: int | None = None,
    ) -> WorkingSession:
        budget = token_budget or self.settings.working_memory_token_budget
        if budget <= 0:
            raise ValueError("token_budget must be positive")
        if self.settings.working_memory_reserved_tokens >= budget:
            raise AppError("INVALID_TOKEN_BUDGET", "Token 预算必须大于预留额度", 422)
        now = self.clock()
        working_session = WorkingSession(
            session_id=session_id or uuid4(),
            actor_id=actor_id,
            status=WorkingSessionStatus.ACTIVE,
            summary="",
            summary_version=None,
            token_budget=budget,
            token_count=0,
            last_activity_at=now,
            expires_at=None,
        )
        self.session.add(working_session)
        self.session.flush()
        return working_session

    def append_item(
        self,
        working_session: WorkingSession,
        *,
        role: str,
        content: str,
        importance: float = 0.5,
        explicit_keep: bool = False,
    ) -> WorkingItem:
        if working_session.status != WorkingSessionStatus.ACTIVE:
            raise AppError("SESSION_CLOSED", "工作会话已关闭", 409)
        normalized = content.strip()
        if not normalized:
            raise ValueError("content must not be empty")
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("unsupported role")
        if not 0 <= importance <= 1:
            raise ValueError("importance must be within [0,1]")
        item = WorkingItem(
            session_id=working_session.session_id,
            sequence_no=self._next_sequence(working_session.session_id),
            role=role,
            content=normalized,
            token_count=estimate_tokens(normalized),
            importance=importance,
            explicit_keep=explicit_keep,
        )
        self.session.add(item)
        working_session.token_count += item.token_count
        working_session.last_activity_at = self.clock()
        self.session.flush()
        if should_roll_summary(
            working_session.token_count,
            working_session.token_budget,
            self.settings.working_memory_summary_ratio,
        ):
            self._roll_summary(working_session)
        return item

    def consolidate(self, working_session: WorkingSession) -> ConsolidationResult:
        if working_session.status != WorkingSessionStatus.ACTIVE:
            raise AppError("SESSION_CLOSED", "工作会话已关闭", 409)
        if self.event_writer is None:
            raise AppError("EVENT_PIPELINE_UNAVAILABLE", "事件管道未配置", 503)
        items = self._session_items(working_session.session_id)
        candidates = [
            item
            for item in items
            if item.consolidated_event_id is None
            and is_consolidation_candidate(
                item, important_threshold=self.settings.working_memory_important_threshold
            )
        ]
        grouped: dict[str, list[WorkingItem]] = {}
        for item in sorted(candidates, key=lambda value: value.sequence_no):
            grouped.setdefault(content_digest(item.content), []).append(item)
        drafts: list[ConsolidationDraft] = []
        event_ids: list[UUID] = []
        for group in grouped.values():
            representative = group[0]
            event_type = classify_consolidation(representative) or "fact"
            draft = ConsolidationDraft(
                session_id=working_session.session_id,
                item_ids=tuple(item.item_id for item in group),
                role=representative.role,
                content=normalize_content(representative.content),
                event_type=event_type,
                importance=max(item.importance for item in group),
                explicit_keep=any(item.explicit_keep for item in group),
                content_hash=content_digest(representative.content),
            )
            event_id = self.event_writer(draft)
            for item in group:
                item.consolidated_event_id = event_id
            drafts.append(draft)
            event_ids.append(event_id)
        now = self.clock()
        working_session.status = WorkingSessionStatus.CLOSED
        working_session.last_activity_at = now
        working_session.expires_at = now + timedelta(hours=self.settings.working_session_ttl_hours)
        self.session.flush()
        return ConsolidationResult(
            event_ids=event_ids,
            item_count=sum(len(group) for group in grouped.values()),
            drafts=drafts,
        )

    def close_session(self, working_session: WorkingSession) -> None:
        if working_session.status == WorkingSessionStatus.CLOSED:
            return
        now = self.clock()
        working_session.status = WorkingSessionStatus.CLOSED
        working_session.last_activity_at = now
        working_session.expires_at = now + timedelta(hours=self.settings.working_session_ttl_hours)
        self.session.flush()

    def _next_sequence(self, session_id: UUID) -> int:
        current = self.session.scalar(
            select(func.max(WorkingItem.sequence_no)).where(WorkingItem.session_id == session_id)
        )
        return int(current or 0) + 1

    def _session_items(self, session_id: UUID) -> list[WorkingItem]:
        return list(
            self.session.scalars(
                select(WorkingItem)
                .where(WorkingItem.session_id == session_id)
                .order_by(WorkingItem.sequence_no)
            ).all()
        )

    def _roll_summary(self, working_session: WorkingSession) -> None:
        items = self._session_items(working_session.session_id)
        _, summarized = select_summary_items(
            items,
            keep_latest=self.settings.working_memory_verbatim_items,
            important_threshold=self.settings.working_memory_important_threshold,
        )
        if not summarized:
            return
        working_session.summary = compose_rolling_summary(summarized)[:8000]
        working_session.summary_version = ROLLING_SUMMARY_VERSION
        self.session.flush()
