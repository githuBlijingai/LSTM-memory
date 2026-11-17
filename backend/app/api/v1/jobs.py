from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_settings_dependency, get_uow, idempotency_key
from app.api.idempotent import execute_idempotent
from app.core.config import Settings
from app.core.security import CurrentUser, require_viewer
from app.models import BackgroundJob, BackgroundJobType
from app.schemas.memory import JobResponse
from app.schemas.responses import success_payload
from app.services.memory_reads import MemoryReadService
from app.services.uow import UnitOfWork
from app.tasks.dispatch import (
    dispatch_event_pipeline,
    dispatch_state_rebuild,
    dispatch_state_replay,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_response(job: BackgroundJob) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        job_type=job.job_type.value,
        resource_type=job.resource_type,
        resource_id=job.resource_id,
        status=job.status.value,
        progress=job.progress,
        attempt_count=job.attempt_count,
        last_error=job.last_error,
        requested_by=job.requested_by,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/{job_id}")
def get_job(
    job_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
) -> dict:
    job = MemoryReadService(uow.session, settings).get_job(user, job_id)
    return success_payload(_job_response(job).model_dump(mode="json"))


@router.post("/{job_id}/retry", status_code=202)
def retry_job(
    job_id: UUID,
    request: Request,
    uow: UnitOfWork = Depends(get_uow),
    settings: Settings = Depends(get_settings_dependency),
    user: CurrentUser = Depends(require_viewer),
    key: str = Depends(idempotency_key),
) -> dict:
    service = MemoryReadService(uow.session, settings)
    holder: dict = {}

    def action():
        job = service.retry_job(user, job_id)
        holder["job"] = job
        return _job_response(job), job.job_id

    body, _ = execute_idempotent(
        uow=uow,
        user=user,
        key=key,
        method=request.method,
        path=request.url.path,
        body={},
        action=action,
    )
    job = holder.get("job")
    if job is None:
        return body
    if job.job_type == BackgroundJobType.STATE_REBUILD:
        dispatch_state_rebuild(job.resource_id, job.job_id)
    elif job.job_type == BackgroundJobType.EVENT_PIPELINE:
        dispatch_event_pipeline(job.resource_id)
    elif job.job_type == BackgroundJobType.REPLAY:
        payload = job.payload if isinstance(job.payload, dict) else {}
        event_id = payload.get("event_id")
        target_version = payload.get("target_state_version")
        if event_id is not None and target_version is not None:
            dispatch_state_replay(UUID(str(event_id)), int(target_version))
    return body
