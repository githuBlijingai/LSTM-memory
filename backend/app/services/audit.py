from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.logging import redact_secrets
from app.core.request_context import get_request_id
from app.models import AuditLog


class AuditLogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor_id: str,
        action: str,
        resource_type: str,
        resource_id: str | UUID,
        reason: str,
        before: dict | list | BaseModel | None = None,
        after: dict | list | BaseModel | None = None,
        request_id: UUID | None = None,
    ) -> AuditLog:
        if not reason.strip():
            raise ValueError("audit reason is required")
        entry = AuditLog(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            before_json=self._safe_json(before),
            after_json=self._safe_json(after),
            reason=reason.strip()[:2000],
            request_id=request_id or get_request_id(),
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    @staticmethod
    def _safe_json(value: dict | list | BaseModel | None) -> Any:
        if value is None:
            return None
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return redact_secrets(value)
