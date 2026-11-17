from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import (
    get_model_bundle,
    get_settings_dependency,
    get_uow,
    idempotency_key,
)
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.rate_limit import rate_limit
from app.core.security import CurrentUser, require_admin, require_viewer
from app.ml.registry import ModelBundle
from app.schemas.memory import (
    EventAcceptedResponse,
    EventCorrectionRequest,
    EventCorrectionResponse,
    EventCreateRequest,
    EventDeleteRequest,
    EventDeleteResponse,
    EventDetailResponse,
    EventRestoreRequest,
    EventRestoreResponse,
    FeedbackRequest,
    FeedbackResponse,
    SearchRequest,
    SearchResponse,
)
from app.schemas.responses import success_payload
from app.services.feedback import FeedbackService
from app.services.memory_reads import MemoryReadService
from app.services.memory_search import MemorySearchService
from app.services.memory_writes import EventCommandService
from app.services.uow import UnitOfWork
from app.tasks.dispatch import dispatch_state_replay

router = APIRouter(prefix="/memory", tags=["memory"])

_search_rate_limit = rate_limit("search", "rate_limit_search_per_minute", 60, require_viewer)
_feedback_rate_limit = rate_limit("feedback", "rate_limit_feedback_per_minute", 60, require_viewer)


@router.post("/events", status_code=202)
def create_event(
    payload: EventCreateRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = EventCommandService(uow.session, settings)

    def action():
        outcome = service.create_event(user, payload)
        response = EventAcceptedResponse(
            event_id=outcome.event.event_id,
            status=outcome.event.status.value,
            job_id=outcome.job.job_id,
        )
        return response, outcome.event.event_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
    )[0]


@router.get("/events")
def list_events(
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    subject: str | None = Query(default=None, max_length=128),
    entity_id: UUID | None = None,
    event_type: str | None = Query(default=None, max_length=64),
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    status: str | None = Query(default=None, max_length=32),
    owner_id: str | None = Query(default=None, max_length=128),
    sort: Literal["event_time.desc", "event_time.asc"] = Query(default="event_time.desc"),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    page = MemoryReadService(uow.session, settings).list_events(
        user,
        subject=subject,
        entity_id=entity_id,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        status=status,
        owner_id=owner_id,
        descending=sort == "event_time.desc",
        cursor=cursor,
        limit=limit,
    )
    return success_payload([item.model_dump(mode="json") for item in page.items], page.next_cursor)


@router.get("/events/{event_id}")
def get_event(
    event_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    detail: EventDetailResponse = MemoryReadService(uow.session, settings).get_event(user, event_id)
    return success_payload(detail.model_dump(mode="json"))


@router.post("/events/{event_id}/corrections", status_code=202)
def correct_event(
    event_id: UUID,
    payload: EventCorrectionRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = EventCommandService(uow.session, settings)

    def action():
        outcome = service.correct_event(user, event_id, payload)
        response = EventCorrectionResponse(
            new_event_id=outcome.event.event_id,
            status=outcome.event.status.value,
            rebuild_job_id=outcome.rebuild_job.job_id if outcome.rebuild_job else None,
        )
        return response, outcome.event.event_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
    )[0]


@router.delete("/events/{event_id}", status_code=202)
def delete_event(
    event_id: UUID,
    payload: EventDeleteRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = EventCommandService(uow.session, settings)

    def action():
        outcome = service.delete_event(user, event_id, payload)
        response = EventDeleteResponse(
            event_id=outcome.event.event_id,
            status=outcome.event.status.value,
            purge_after=outcome.event.purge_after,
            rebuild_job_id=outcome.rebuild_job.job_id if outcome.rebuild_job else None,
        )
        return response, outcome.event.event_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
    )[0]


@router.post("/events/{event_id}/restore", status_code=202)
def restore_event(
    event_id: UUID,
    payload: EventRestoreRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_admin),
    key: str = Depends(idempotency_key),
) -> dict:
    service = EventCommandService(uow.session, settings)

    def action():
        outcome = service.restore_event(user, event_id, payload.reason)
        response = EventRestoreResponse(
            event_id=outcome.event.event_id,
            status=outcome.event.status.value,
            rebuild_job_id=outcome.rebuild_job.job_id if outcome.rebuild_job else None,
        )
        return response, outcome.event.event_id

    return execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
    )[0]


@router.post("/search", dependencies=[Depends(_search_rate_limit)])
def search_memory(
    payload: SearchRequest,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    models: ModelBundle = Depends(get_model_bundle),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    outcome = MemorySearchService(uow.session, settings, models).search(user=user, request=payload)
    uow.commit()
    response = SearchResponse(
        query_id=outcome.query_id,
        intent=outcome.intent,
        results=outcome.results,
    )
    return success_payload(response.model_dump(mode="json"))


@router.post("/feedback", dependencies=[Depends(_feedback_rate_limit)])
def submit_feedback(
    payload: FeedbackRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = FeedbackService(uow.session, settings)
    outcome_holder: dict = {}

    def action():
        outcome = service.submit(user=user, request=payload)
        outcome_holder["replay"] = (outcome.replay_event_id, outcome.replay_before_state_version)
        response = FeedbackResponse(**outcome.response.model_dump())
        return response, payload.memory_id

    payload_body, status = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
        status_code=200,
    )
    replay = outcome_holder.get("replay")
    if replay and replay[0] is not None and replay[1] is not None:
        dispatch_state_replay(replay[0], replay[1])
    return payload_body
