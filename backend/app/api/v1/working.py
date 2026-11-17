from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.dependencies import get_settings_dependency, get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.security import CurrentUser, require_viewer
from app.models import WorkingItem, WorkingSession
from app.schemas.memory import (
    ConsolidateEventRef,
    WorkingCloseResponse,
    WorkingConsolidateResponse,
    WorkingItemAppendRequest,
    WorkingItemAppendResponse,
    WorkingItemResponse,
    WorkingSessionCreateRequest,
    WorkingSessionDetailResponse,
    WorkingSessionResponse,
)
from app.schemas.responses import success_payload
from app.services.memory_writes import EventCommandService
from app.services.uow import UnitOfWork
from app.services.working_memory import WorkingMemoryService, should_roll_summary

router = APIRouter(prefix="/working", tags=["working-memory"])


def _session_response(session: WorkingSession) -> WorkingSessionResponse:
    return WorkingSessionResponse(
        session_id=session.session_id,
        actor_id=session.actor_id,
        status=session.status.value,
        summary=session.summary,
        summary_version=session.summary_version,
        token_budget=session.token_budget,
        token_count=session.token_count,
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
    )


def _item_response(item: WorkingItem) -> WorkingItemResponse:
    return WorkingItemResponse(
        item_id=item.item_id,
        sequence_no=item.sequence_no,
        role=item.role,
        content=item.content,
        token_count=item.token_count,
        importance=item.importance,
        explicit_keep=item.explicit_keep,
        consolidated_event_id=item.consolidated_event_id,
        created_at=item.created_at,
    )


def _load_session(uow: UnitOfWork, user: CurrentUser, session_id: UUID) -> WorkingSession:
    session = uow.session.get(WorkingSession, session_id)
    if session is None:
        raise NotFoundError("工作记忆会话不存在")
    if session.actor_id != user.subject and not user.is_admin:
        raise AppError("FORBIDDEN", "不能访问其他用户的工作记忆会话", 403)
    return session


@router.post("/sessions", status_code=201)
def create_session(
    payload: WorkingSessionCreateRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = WorkingMemoryService(uow.session, settings)

    def action():
        session = service.create_session(user.subject, token_budget=payload.token_budget)
        return _session_response(session), session.session_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
        status_code=201,
    )[0]


@router.get("/sessions/{session_id}")
def get_session(
    session_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    session = _load_session(uow, user, session_id)
    items = list(
        uow.session.scalars(
            select(WorkingItem)
            .where(WorkingItem.session_id == session_id)
            .order_by(WorkingItem.sequence_no)
        ).all()
    )
    detail = WorkingSessionDetailResponse(
        **_session_response(session).model_dump(),
        items=[_item_response(item) for item in items],
    )
    return success_payload(detail.model_dump(mode="json"))


@router.post("/sessions/{session_id}/items", status_code=201)
def append_item(
    session_id: UUID,
    payload: WorkingItemAppendRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = WorkingMemoryService(uow.session, settings)

    def action():
        session = _load_session(uow, user, session_id)
        before_tokens = session.token_count
        item = service.append_item(
            session,
            role=payload.role,
            content=payload.content,
            importance=payload.importance,
            explicit_keep=payload.explicit_keep,
        )
        triggered = not should_roll_summary(
            before_tokens, session.token_budget, settings.working_memory_summary_ratio
        ) and should_roll_summary(
            session.token_count, session.token_budget, settings.working_memory_summary_ratio
        )
        response = WorkingItemAppendResponse(
            session_id=session.session_id,
            item_id=item.item_id,
            token_count=item.token_count,
            session_token_count=session.token_count,
            summary_triggered=triggered,
            summary_version=session.summary_version,
        )
        return response, item.item_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
        status_code=201,
    )[0]


@router.post("/sessions/{session_id}/consolidate", status_code=202)
def consolidate_session(
    session_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    command_service = EventCommandService(uow.session, settings)
    service = WorkingMemoryService(
        uow.session,
        settings,
        event_writer=lambda draft: command_service.create_consolidated_event(user, draft),
    )

    def action():
        session = _load_session(uow, user, session_id)
        result = service.consolidate(session)
        response = WorkingConsolidateResponse(
            session_id=session.session_id,
            events=[
                ConsolidateEventRef(event_id=event_id, status="pending")
                for event_id in result.event_ids
            ],
        )
        return response, session.session_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
    )[0]


@router.delete("/sessions/{session_id}")
def close_session(
    session_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = WorkingMemoryService(uow.session, settings)

    def action():
        session = _load_session(uow, user, session_id)
        service.close_session(session)
        response = WorkingCloseResponse(
            session_id=session.session_id,
            status=session.status.value,
            expires_at=session.expires_at,
        )
        return response, session.session_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
        status_code=200,
    )[0]
