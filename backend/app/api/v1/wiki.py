from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.dependencies import get_settings_dependency, get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.rate_limit import rate_limit
from app.core.security import CurrentUser, require_operator, require_viewer
from app.schemas.responses import success_payload
from app.schemas.wiki import WikiSyncTaskCreateRequest
from app.services.uow import UnitOfWork
from app.services.wiki_reads import WikiReadService
from app.tasks.dispatch import dispatch_wiki_sync

router = APIRouter(prefix="/wiki", tags=["wiki"])

_sync_create_rate_limit = rate_limit(
    "wiki_sync", "rate_limit_sync_per_hour", 3600, require_operator
)


@router.get("/articles")
def list_articles(
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=128),
    language: str | None = Query(default=None, max_length=16),
    status: str | None = Query(default=None, max_length=32),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    page = WikiReadService(uow.session, settings).list_articles(
        user,
        query=q,
        category=category,
        language=language,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return success_payload([item.model_dump(mode="json") for item in page.items], page.next_cursor)


@router.get("/articles/{article_id}")
def get_article(
    article_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    include_inactive: bool = Query(default=False),
) -> dict:
    detail = WikiReadService(uow.session, settings).get_article(
        user, article_id, include_inactive=include_inactive
    )
    return success_payload(detail.model_dump(mode="json"))


@router.get("/articles/{article_id}/chunks/{chunk_index}")
def get_chunk(
    article_id: UUID,
    chunk_index: int,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    include_inactive: bool = Query(default=False),
) -> dict:
    detail = WikiReadService(uow.session, settings).get_chunk(
        user, article_id, chunk_index, include_inactive=include_inactive
    )
    return success_payload(detail.model_dump(mode="json"))


@router.post("/sync-tasks", status_code=202, dependencies=[Depends(_sync_create_rate_limit)])
def create_sync_task(
    payload: WikiSyncTaskCreateRequest,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_operator),
    key: str = Depends(idempotency_key),
) -> dict:
    service = WikiReadService(uow.session, settings)
    dispatched: dict[str, UUID] = {}

    def action():
        response, job_id = service.create_sync_task(user, payload)
        dispatched["task_id"] = response.task_id
        dispatched["job_id"] = job_id
        return response, response.task_id

    body, status = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body=payload.model_dump(mode="json"),
        action=action,
    )
    if "task_id" in dispatched:
        dispatch_wiki_sync(dispatched["task_id"], dispatched["job_id"])
    return body


@router.get("/sync-tasks")
def list_sync_tasks(
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    source: str | None = Query(default=None, max_length=64),
    mode: str | None = Query(default=None, max_length=32),
    status: str | None = Query(default=None, max_length=32),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    page = WikiReadService(uow.session, settings).list_sync_tasks(
        user,
        source=source,
        mode=mode,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return success_payload([item.model_dump(mode="json") for item in page.items], page.next_cursor)


@router.get("/sync-tasks/{task_id}")
def get_sync_task(
    task_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    response_model = WikiReadService(uow.session, settings).get_sync_task(user, task_id)
    return success_payload(response_model.model_dump(mode="json"))


@router.post("/sync-tasks/{task_id}/retry", status_code=202)
def retry_sync_task(
    task_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_operator),
    key: str = Depends(idempotency_key),
) -> dict:
    service = WikiReadService(uow.session, settings)
    dispatched: dict[str, UUID] = {}

    def action():
        response, job_id = service.retry_sync_task(user, task_id)
        dispatched["task_id"] = response.task_id
        dispatched["job_id"] = job_id
        return response, response.task_id

    body, status = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
    )
    if "task_id" in dispatched:
        dispatch_wiki_sync(dispatched["task_id"], dispatched["job_id"])
    return body
