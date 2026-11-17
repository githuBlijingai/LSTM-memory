from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import EventMemory, IdempotencyRecord
from app.repositories.base import (
    apply_cursor,
    decode_cursor,
    encode_cursor,
    paginate,
)


def test_cursor_round_trip_normalizes_utc() -> None:
    timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    record_id = uuid4()

    cursor = encode_cursor(timestamp, record_id)
    decoded_time, decoded_id = decode_cursor(cursor)

    assert decoded_time == timestamp.astimezone(UTC)
    assert decoded_time.tzinfo == UTC
    assert decoded_id == record_id


@pytest.mark.parametrize("cursor", ["not-base64", "", "e30"])
def test_invalid_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        decode_cursor(cursor)


def test_naive_cursor_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_cursor(datetime(2026, 1, 1), uuid4())


def test_cursor_predicate_has_stable_tiebreaker() -> None:
    cursor = encode_cursor(datetime.now(UTC), uuid4())

    statement = apply_cursor(select(EventMemory), EventMemory, cursor)
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "event_memory.created_at <" in sql
    assert "event_memory.created_at =" in sql
    assert "event_memory.event_id <" in sql
    with pytest.raises(ValueError, match="limit"):
        paginate(None, EventMemory, limit=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="single-column primary key"):
        apply_cursor(select(IdempotencyRecord), IdempotencyRecord, cursor)
