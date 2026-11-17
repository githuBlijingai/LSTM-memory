from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflictError
from app.models import IdempotencyRecord
from app.schemas.json_fields import IdempotencyResponseBody, to_json_dict


def hash_request(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(
        {"method": method.upper(), "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class IdempotencyResult:
    record: IdempotencyRecord
    replay: bool


class IdempotencyService:
    def __init__(
        self,
        session: Session,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention: timedelta = timedelta(hours=24),
    ) -> None:
        self.session = session
        self.clock = clock
        self.retention = retention

    def begin(
        self, *, actor_id: str, key: str, request_path: str, request_hash: str
    ) -> IdempotencyResult:
        if not key or len(key) > 128:
            raise ValueError("idempotency key must contain 1 to 128 characters")
        now = self.clock()
        record = self.session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.key == key,
                IdempotencyRecord.request_path == request_path,
            )
            .with_for_update()
        )
        if record is not None and record.expires_at > now:
            if record.request_hash != request_hash:
                raise IdempotencyConflictError()
            return IdempotencyResult(record=record, replay=record.response_status is not None)
        if record is not None:
            self.session.delete(record)
            self.session.flush()
        record = IdempotencyRecord(
            actor_id=actor_id,
            key=key,
            request_path=request_path,
            request_hash=request_hash,
            expires_at=now + self.retention,
        )
        self.session.add(record)
        self.session.flush()
        return IdempotencyResult(record=record, replay=False)

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        response_status: int,
        response_body: IdempotencyResponseBody | BaseModel,
        resource_id: UUID | None = None,
    ) -> None:
        if not 100 <= response_status <= 599:
            raise ValueError("invalid HTTP response status")
        record.response_status = response_status
        record.response_body = to_json_dict(response_body)
        record.resource_id = resource_id
        self.session.flush()
