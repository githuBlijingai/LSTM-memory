from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_settings_dependency, get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.security import CurrentUser, require_admin
from app.schemas.memory import FamiliarityUpdateRequest, FamiliarityUpdateResponse
from app.services.memory_reads import MemoryReadService
from app.services.uow import UnitOfWork

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/memories/{memory_type}/{memory_id}/familiarity")
def update_familiarity(
    memory_type: str,
    memory_id: UUID,
    payload: FamiliarityUpdateRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = MemoryReadService(uow.session, settings)

    def action():
        memory = service.set_familiarity(
            user, memory_type, memory_id, payload.score, payload.reason
        )
        response = FamiliarityUpdateResponse(
            memory_type="wiki" if memory_type == "wiki" else "event",
            memory_id=memory_id,
            familiarity_score=memory.familiarity_score,
            updated_at=memory.familiarity_updated_at,
        )
        return response, memory_id

    body, _ = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
        status_code=200,
    )
    return body
