from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, true
from sqlalchemy.sql.elements import ColumnElement

from app.core.security import CurrentUser
from app.models import EventMemory, Visibility


def visibility_predicate(
    user: CurrentUser,
    owner_column: ColumnElement[str],
    visibility_column: ColumnElement[Visibility],
) -> ColumnElement[bool]:
    if user.is_admin:
        return true()
    return or_(
        visibility_column == Visibility.SHARED,
        and_(
            visibility_column == Visibility.PRIVATE,
            owner_column == user.subject,
        ),
    )


def event_candidate_predicate(user: CurrentUser) -> ColumnElement[bool]:
    return and_(
        EventMemory.status == "active",
        EventMemory.embedding.is_not(None),
        EventMemory.deleted_at.is_(None),
        visibility_predicate(user, EventMemory.owner_id, EventMemory.visibility),
    )


def can_view_memory(user: CurrentUser, memory: Any) -> bool:
    return (
        user.is_admin
        or memory.visibility == Visibility.SHARED
        or memory.visibility == "shared"
        or (
            (memory.visibility == Visibility.PRIVATE or memory.visibility == "private")
            and memory.owner_id == user.subject
        )
    )


def can_view_wiki(user: CurrentUser) -> bool:
    return bool(user.roles)
