from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class OutboxDeadLetterItem(BaseModel):
    outbox_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    status: str
    attempt_count: int
    last_error: str | None
    available_at: datetime
    created_at: datetime
    processed_at: datetime | None


class OutboxReplayResponse(BaseModel):
    outbox_id: UUID
    status: str
    attempt_count: int
    available_at: datetime
