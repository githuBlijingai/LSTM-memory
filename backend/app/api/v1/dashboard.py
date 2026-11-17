from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings_dependency, get_uow
from app.core.config import Settings
from app.core.security import CurrentUser, require_viewer
from app.schemas.responses import success_payload
from app.services.memory_reads import MemoryReadService
from app.services.uow import UnitOfWork

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    summary = MemoryReadService(uow.session, settings).dashboard_summary(user)
    return success_payload(summary.model_dump(mode="json"))
