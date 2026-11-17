from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.core.errors import AppError, IdempotencyConflictError
from app.models import (
    AuditLog,
    BackgroundJobType,
    IdempotencyRecord,
    JobStatus,
    OutboxStatus,
)
from app.schemas.json_fields import BackgroundJobPayload, IdempotencyResponseBody, OutboxPayload
from app.services.audit import AuditLogService
from app.services.idempotency import IdempotencyService, hash_request
from app.services.jobs import BackgroundJobService
from app.services.outbox import OutboxService


class FakeScalars:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


class FakeSession:
    def __init__(
        self,
        *,
        record: IdempotencyRecord | None = None,
        batch: list[Any] | None = None,
    ) -> None:
        self.record = record
        self.batch = batch or []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flush_count = 0

    def scalar(self, statement: Any) -> IdempotencyRecord | None:
        return self.record

    def scalars(self, statement: Any) -> FakeScalars:
        return FakeScalars(self.batch)

    def add(self, value: Any) -> None:
        self.added.append(value)

    def delete(self, value: Any) -> None:
        self.deleted.append(value)

    def flush(self) -> None:
        self.flush_count += 1


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_idempotency_replay_completion_and_conflict() -> None:
    body = {"title": "event"}
    request_hash = hash_request("POST", "/events", body)
    record = IdempotencyRecord(
        key="request-key",
        actor_id="alice",
        request_path="/events",
        request_hash=request_hash,
        expires_at=NOW + timedelta(hours=1),
    )
    session = FakeSession(record=record)
    service = IdempotencyService(session, clock=lambda: NOW)  # type: ignore[arg-type]

    first = service.begin(
        actor_id="alice", key="request-key", request_path="/events", request_hash=request_hash
    )
    assert not first.replay
    service.complete(
        record,
        response_status=201,
        response_body=IdempotencyResponseBody(data={"ok": True}, request_id=uuid4()),
        resource_id=uuid4(),
    )
    replay = service.begin(
        actor_id="alice", key="request-key", request_path="/events", request_hash=request_hash
    )

    assert replay.replay
    assert isinstance(record.response_body, dict)
    with pytest.raises(IdempotencyConflictError):
        service.begin(
            actor_id="alice",
            key="request-key",
            request_path="/events",
            request_hash=hash_request("POST", "/events", {"title": "different"}),
        )


def test_expired_idempotency_record_is_replaced() -> None:
    record = IdempotencyRecord(
        key="old",
        actor_id="alice",
        request_path="/events",
        request_hash="hash",
        expires_at=NOW - timedelta(seconds=1),
    )
    session = FakeSession(record=record)
    service = IdempotencyService(session, clock=lambda: NOW)  # type: ignore[arg-type]

    result = service.begin(
        actor_id="alice", key="old", request_path="/events", request_hash="new-hash"
    )

    assert session.deleted == [record]
    assert result.record in session.added
    assert result.record.request_hash == "new-hash"
    assert result.record.expires_at == NOW + timedelta(hours=24)


def test_outbox_claim_retry_dead_letter_and_redaction() -> None:
    session = FakeSession()
    service = OutboxService(session, clock=lambda: NOW)  # type: ignore[arg-type]
    event = service.enqueue(
        aggregate_type="event",
        aggregate_id=uuid4(),
        event_type="event.created",
        payload=OutboxPayload(operation="encode", resource_id=uuid4()),
    )
    session.batch = [event]

    claimed = service.claim_batch()
    assert claimed == [event]
    assert event.status == OutboxStatus.PROCESSING
    assert event.attempt_count == 1

    service.mark_failed(event, "Bearer secret-token", max_attempts=2)
    assert event.status == OutboxStatus.FAILED
    assert event.available_at == NOW + timedelta(seconds=30)
    assert "secret-token" not in (event.last_error or "")

    event.attempt_count = 2
    service.mark_failed(event, "final failure", max_attempts=2)
    assert event.status == OutboxStatus.DEAD_LETTER
    service.mark_processed(event)
    assert event.status == OutboxStatus.PROCESSED
    assert event.processed_at == NOW
    assert event.last_error is None


def test_background_job_state_transitions() -> None:
    session = FakeSession()
    service = BackgroundJobService(session, clock=lambda: NOW)  # type: ignore[arg-type]
    job = service.create(
        job_type=BackgroundJobType.REPLAY,
        resource_type="entity",
        resource_id=uuid4(),
        payload=BackgroundJobPayload(operation="replay"),
        requested_by="operator",
    )
    job.attempt_count = 0

    service.start(job)
    service.set_progress(job, 0.5)
    service.mark_succeeded(job)

    assert job.status == JobStatus.SUCCEEDED
    assert job.started_at == NOW
    assert job.finished_at == NOW
    assert job.progress == 1.0
    with pytest.raises(AppError) as error:
        service.start(job)
    assert error.value.code == "TASK_CONFLICT"


def test_audit_log_requires_reason_and_redacts_snapshots() -> None:
    session = FakeSession()
    service = AuditLogService(session)  # type: ignore[arg-type]
    request_id = uuid4()

    entry = service.record(
        actor_id="admin",
        action="event.delete",
        resource_type="event",
        resource_id=uuid4(),
        reason=" 合规请求 ",
        before={"api_key": "secret", "content": "safe"},
        after={"nested": {"token": "secret"}},
        request_id=request_id,
    )

    assert isinstance(entry, AuditLog)
    assert entry.before_json == {"api_key": "[REDACTED]", "content": "safe"}
    assert entry.after_json == {"nested": {"token": "[REDACTED]"}}
    assert entry.reason == "合规请求"
    assert entry.request_id == request_id
    with pytest.raises(ValueError, match="reason"):
        service.record(
            actor_id="admin",
            action="event.delete",
            resource_type="event",
            resource_id=uuid4(),
            reason=" ",
        )
