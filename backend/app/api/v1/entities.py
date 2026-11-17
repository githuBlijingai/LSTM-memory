from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import get_settings_dependency, get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.security import CurrentUser, require_admin, require_viewer
from app.schemas.responses import success_payload
from app.services.memory_reads import MemoryReadService
from app.services.uow import UnitOfWork
from app.tasks.dispatch import dispatch_state_rebuild

router = APIRouter(prefix="/entities", tags=["entities"])


@router.get("")
def list_entities(
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    entity_type: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    page = MemoryReadService(uow.session, settings).list_entities(
        user, entity_type=entity_type, status=status, cursor=cursor, limit=limit
    )
    return success_payload([item.model_dump(mode="json") for item in page.items], page.next_cursor)


@router.get("/{entity_id}")
def get_entity(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    detail = MemoryReadService(uow.session, settings).get_entity(user, entity_id)
    return success_payload(detail.model_dump(mode="json"))


@router.get("/{entity_id}/state")
def get_entity_state(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    state = MemoryReadService(uow.session, settings).get_entity_state(user, entity_id)
    return success_payload(state.model_dump(mode="json"))


@router.get("/{entity_id}/events")
def list_entity_events(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    event_type: str | None = Query(default=None, max_length=64),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    page = MemoryReadService(uow.session, settings).list_entity_events(
        user,
        entity_id,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        cursor=cursor,
        limit=limit,
    )
    return success_payload([item.model_dump(mode="json") for item in page.items], page.next_cursor)


@router.get("/{entity_id}/replays")
def list_entity_replays(
    entity_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    replays = MemoryReadService(uow.session, settings).list_replays(user, entity_id, limit=limit)
    return success_payload([item.model_dump(mode="json") for item in replays])


@router.post("/{entity_id}/rebuild", status_code=202)
def rebuild_entity(
    entity_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = MemoryReadService(uow.session, settings)
    holder: dict = {}

    def action():
        result = service.request_rebuild(user, entity_id)
        holder["result"] = result
        return result, entity_id

    body, _ = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
    )
    result = holder.get("result")
    if result is not None:
        dispatch_state_rebuild(result.entity_id, result.job_id)
    return body
