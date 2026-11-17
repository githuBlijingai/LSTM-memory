from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CursorPage[M]:
    items: list[M]
    next_cursor: str | None


def encode_cursor(created_at: datetime, record_id: UUID) -> str:
    if created_at.tzinfo is None:
        raise ValueError("cursor timestamp must be timezone-aware")
    normalized = created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raw = json.dumps(
        {"created_at": normalized, "id": str(record_id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if set(value) != {"created_at", "id"}:
            raise ValueError("unexpected cursor fields")
        created_at = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp has no timezone")
        return created_at.astimezone(UTC), UUID(value["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def apply_cursor[M](
    statement: Select,
    model: type[M],
    cursor: str | None,
    *,
    descending: bool = True,
) -> Select:
    if not cursor:
        return statement
    created_at, record_id = decode_cursor(cursor)
    primary_key = _single_primary_key(model)
    tie = primary_key < record_id if descending else primary_key > record_id
    time_compare = model.created_at < created_at if descending else model.created_at > created_at
    return statement.where(or_(time_compare, and_(model.created_at == created_at, tie)))


def paginate[M](
    session: Session,
    model: type[M],
    *,
    statement: Select | None = None,
    cursor: str | None = None,
    limit: int = 20,
    descending: bool = True,
) -> CursorPage[M]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    primary_key = _single_primary_key(model)
    statement = statement if statement is not None else select(model)
    statement = apply_cursor(statement, model, cursor, descending=descending)
    order = (
        (model.created_at.desc(), primary_key.desc())
        if descending
        else (
            model.created_at.asc(),
            primary_key.asc(),
        )
    )
    rows = list(session.scalars(statement.order_by(*order).limit(limit + 1)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(items[-1].created_at, getattr(items[-1], primary_key.key))
    return CursorPage(items=items, next_cursor=next_cursor)


def _single_primary_key[M](model: type[M]):
    columns = list(model.__table__.primary_key.columns)
    if len(columns) != 1:
        raise TypeError("cursor pagination requires a single-column primary key")
    return getattr(model, columns[0].key)


class BaseRepository[M]:
    def __init__(self, session: Session, model: type[M]) -> None:
        self.session = session
        self.model = model

    def get(self, record_id: UUID, *, for_update: bool = False) -> M | None:
        statement = select(self.model).where(_single_primary_key(self.model) == record_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def add(self, instance: M) -> M:
        self.session.add(instance)
        self.session.flush()
        return instance

    def delete(self, instance: M) -> None:
        self.session.delete(instance)
        self.session.flush()

    def list_page(
        self,
        *,
        statement: Select | None = None,
        cursor: str | None = None,
        limit: int = 20,
        descending: bool = True,
    ) -> CursorPage[M]:
        return paginate(
            self.session,
            self.model,
            statement=statement,
            cursor=cursor,
            limit=limit,
            descending=descending,
        )

    def count(self, statement: Select | None = None) -> int:
        selected = statement if statement is not None else select(self.model)
        return len(list(self.session.scalars(selected).all()))


class SQLAlchemyRepository[M](BaseRepository[M]):
    def __init__(self, session: Session, model: type[M]) -> None:
        super().__init__(session, model)
