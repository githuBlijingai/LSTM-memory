"""Wiki 读取与同步任务管理（AG-04，设计 6.7）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ForbiddenError, NotFoundError
from app.core.security import CurrentUser, Role
from app.models import (
    BackgroundJob,
    BackgroundJobType,
    JobStatus,
    WikiArticle,
    WikiArticleStatus,
    WikiChunk,
    WikiChunkStatus,
    WikiSyncMode,
    WikiSyncTask,
)
from app.repositories.base import CursorPage, paginate
from app.schemas.json_fields import BackgroundJobPayload, OutboxPayload, WikiSyncErrorItem
from app.schemas.memory import WikiSourceRef
from app.schemas.wiki import (
    WikiArticleDetailResponse,
    WikiArticleSummaryResponse,
    WikiChunkDetailResponse,
    WikiChunkSummaryResponse,
    WikiSyncTaskCreateRequest,
    WikiSyncTaskResponse,
    WikiTocEntry,
)
from app.services.audit import AuditLogService
from app.services.jobs import BackgroundJobService
from app.services.outbox import OutboxService

ACTIVE_SYNC_STATUSES = {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRY_WAIT}
RETRYABLE_STATUSES = {JobStatus.FAILED, JobStatus.DEAD_LETTER}


def _chunk_response(chunk: WikiChunk) -> WikiChunkSummaryResponse:
    return WikiChunkSummaryResponse(
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
        heading_path=list(chunk.heading_path or []),
        raw_text=chunk.raw_text,
        token_count=chunk.token_count,
        status=chunk.status.value,
        model_version=chunk.model_version,
    )


class WikiReadService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.settings = settings
        self.clock = clock

    # ---- 词条 -------------------------------------------------------------

    def list_articles(
        self,
        user: CurrentUser,
        *,
        query: str | None,
        category: str | None,
        language: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[WikiArticleSummaryResponse]:
        statement = select(WikiArticle)
        if status is None:
            statement = statement.where(WikiArticle.status == WikiArticleStatus.ACTIVE)
        else:
            if not user.has_role(Role.OPERATOR):
                raise ForbiddenError("只有 operator 及以上角色可以按状态筛选词条")
            try:
                statement = statement.where(WikiArticle.status == WikiArticleStatus(status))
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的词条状态", 422) from exc
        if query:
            statement = statement.where(WikiArticle.title.ilike(f"%{query.strip()}%"))
        if category:
            statement = statement.where(WikiArticle.categories.contains([category]))
        if language:
            statement = statement.where(WikiArticle.language == language)
        page = paginate(self.session, WikiArticle, statement=statement, cursor=cursor, limit=limit)
        counts = self._active_chunk_counts([article.article_id for article in page.items])
        return CursorPage(
            items=[
                self._article_summary(article, counts.get(article.article_id, 0))
                for article in page.items
            ],
            next_cursor=page.next_cursor,
        )

    def get_article(
        self, user: CurrentUser, article_id: UUID, *, include_inactive: bool = False
    ) -> WikiArticleDetailResponse:
        include_inactive = self._allow_inactive(user, include_inactive)
        article = self.session.get(WikiArticle, article_id)
        if article is None:
            raise NotFoundError("Wiki 词条不存在")
        if article.status != WikiArticleStatus.ACTIVE and not include_inactive:
            raise NotFoundError("Wiki 词条不存在")
        chunks = self._article_chunks(article.article_id, include_inactive=include_inactive)
        counts = self._active_chunk_counts([article.article_id])
        return WikiArticleDetailResponse(
            **self._article_summary(article, counts.get(article.article_id, 0)).model_dump(),
            content=article.content,
            toc=self._toc(article, chunks, include_inactive=include_inactive),
            chunks=[_chunk_response(chunk) for chunk in chunks],
        )

    def get_chunk(
        self,
        user: CurrentUser,
        article_id: UUID,
        chunk_index: int,
        *,
        include_inactive: bool = False,
    ) -> WikiChunkDetailResponse:
        include_inactive = self._allow_inactive(user, include_inactive)
        article = self.session.get(WikiArticle, article_id)
        if article is None:
            raise NotFoundError("Wiki 词条不存在")
        if article.status != WikiArticleStatus.ACTIVE and not include_inactive:
            raise NotFoundError("Wiki 词条不存在")
        chunks = self._article_chunks(article.article_id, include_inactive=include_inactive)
        positions = [
            index for index, chunk in enumerate(chunks) if chunk.chunk_index == chunk_index
        ]
        if not positions:
            raise NotFoundError("Wiki 知识块不存在")
        position = positions[-1]
        chunk = chunks[position]
        return WikiChunkDetailResponse(
            article_id=article.article_id,
            article_title=article.title,
            chunk=_chunk_response(chunk),
            previous_chunk_index=chunks[position - 1].chunk_index if position > 0 else None,
            next_chunk_index=(
                chunks[position + 1].chunk_index if position + 1 < len(chunks) else None
            ),
            source=WikiSourceRef(
                name=article.source,
                url=article.source_url,
                page_id=article.source_page_id,
                revision_id=article.revision_id,
            ),
        )

    # ---- 同步任务 ---------------------------------------------------------

    def create_sync_task(
        self, user: CurrentUser, request: WikiSyncTaskCreateRequest
    ) -> tuple[WikiSyncTaskResponse, UUID]:
        self._require_operator(user)
        running = self.session.scalar(
            select(WikiSyncTask.task_id).where(
                WikiSyncTask.source == request.source,
                WikiSyncTask.status.in_(list(ACTIVE_SYNC_STATUSES)),
            )
        )
        if running is not None:
            raise AppError("TASK_CONFLICT", "该来源已有运行中的同步任务", 409)
        task = WikiSyncTask(
            source=request.source,
            mode=request.mode,
            package_version=request.package_version,
            status=JobStatus.QUEUED,
            requested_by=user.subject,
        )
        self.session.add(task)
        self.session.flush()
        job = BackgroundJobService(self.session, self.clock).create(
            job_type=BackgroundJobType.WIKI_SYNC,
            resource_type="wiki_sync_task",
            resource_id=task.task_id,
            payload=BackgroundJobPayload(
                operation="wiki_sync",
                source=request.source,
                package_version=request.package_version,
                package_uri=request.package_uri,
            ),
            requested_by=user.subject,
        )
        OutboxService(self.session, self.clock).enqueue(
            aggregate_type="wiki_sync_task",
            aggregate_id=task.task_id,
            event_type="wiki.sync.requested",
            payload=OutboxPayload(
                resource_id=task.task_id,
                job_id=job.job_id,
                operation="wiki_sync",
                source=request.source,
                package_version=request.package_version,
                package_uri=request.package_uri,
            ),
        )
        AuditLogService(self.session).record(
            actor_id=user.subject,
            action="wiki.sync.create",
            resource_type="wiki_sync_task",
            resource_id=task.task_id,
            reason=f"创建 Wiki {request.mode.value} 同步任务",
            after={
                "source": request.source,
                "mode": request.mode.value,
                "package_version": request.package_version,
            },
        )
        self.session.flush()
        return self._task_response(task, job), job.job_id

    def list_sync_tasks(
        self,
        user: CurrentUser,
        *,
        source: str | None,
        mode: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> CursorPage[WikiSyncTaskResponse]:
        statement = select(WikiSyncTask)
        if source:
            statement = statement.where(WikiSyncTask.source == source)
        if mode:
            try:
                statement = statement.where(WikiSyncTask.mode == WikiSyncMode(mode))
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的同步模式", 422) from exc
        if status:
            try:
                statement = statement.where(WikiSyncTask.status == JobStatus(status))
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", "无效的任务状态", 422) from exc
        page = paginate(self.session, WikiSyncTask, statement=statement, cursor=cursor, limit=limit)
        jobs = self._task_jobs([task.task_id for task in page.items])
        return CursorPage(
            items=[self._task_response(task, jobs.get(task.task_id)) for task in page.items],
            next_cursor=page.next_cursor,
        )

    def get_sync_task(self, user: CurrentUser, task_id: UUID) -> WikiSyncTaskResponse:
        task = self.session.get(WikiSyncTask, task_id)
        if task is None:
            raise NotFoundError("Wiki 同步任务不存在")
        job = self._task_jobs([task.task_id]).get(task.task_id)
        return self._task_response(task, job)

    def retry_sync_task(
        self, user: CurrentUser, task_id: UUID
    ) -> tuple[WikiSyncTaskResponse, UUID]:
        self._require_operator(user)
        task = self.session.get(WikiSyncTask, task_id, with_for_update=True)
        if task is None:
            raise NotFoundError("Wiki 同步任务不存在")
        if task.status not in RETRYABLE_STATUSES:
            raise AppError("TASK_CONFLICT", "只有失败任务可以重试", 409)
        job = self._task_jobs([task.task_id]).get(task.task_id)
        service = BackgroundJobService(self.session, self.clock)
        if job is None or job.status not in RETRYABLE_STATUSES:
            job = service.create(
                job_type=BackgroundJobType.WIKI_SYNC,
                resource_type="wiki_sync_task",
                resource_id=task.task_id,
                payload=BackgroundJobPayload(
                    operation="wiki_sync",
                    source=task.source,
                    package_version=task.package_version,
                    package_uri=self._package_uri(job),
                    cursor=task.cursor,
                ),
                requested_by=user.subject,
            )
        else:
            service.retry(job)
        task.status = JobStatus.QUEUED
        task.started_at = None
        task.finished_at = None
        OutboxService(self.session, self.clock).enqueue(
            aggregate_type="wiki_sync_task",
            aggregate_id=task.task_id,
            event_type="wiki.sync.requested",
            payload=OutboxPayload(
                resource_id=task.task_id,
                job_id=job.job_id,
                operation="wiki_sync",
                source=task.source,
                package_version=task.package_version,
                cursor=task.cursor,
            ),
        )
        AuditLogService(self.session).record(
            actor_id=user.subject,
            action="wiki.sync.retry",
            resource_type="wiki_sync_task",
            resource_id=task.task_id,
            reason="重试 Wiki 同步任务",
            after={"cursor": task.cursor},
        )
        self.session.flush()
        return self._task_response(task, job), job.job_id

    # ---- 内部 -------------------------------------------------------------

    def _require_operator(self, user: CurrentUser) -> None:
        if not user.has_role(Role.OPERATOR):
            raise ForbiddenError("需要 operator 及以上角色")

    @staticmethod
    def _allow_inactive(user: CurrentUser, include_inactive: bool) -> bool:
        if not include_inactive:
            return False
        if not user.has_role(Role.OPERATOR):
            raise ForbiddenError("只有 operator 及以上角色可以查看非活跃知识块")
        return True

    def _article_chunks(self, article_id: UUID, *, include_inactive: bool) -> list[WikiChunk]:
        statement = select(WikiChunk).where(WikiChunk.article_id == article_id)
        if not include_inactive:
            statement = statement.where(WikiChunk.status == WikiChunkStatus.ACTIVE)
        return list(
            self.session.scalars(
                statement.order_by(WikiChunk.chunk_index, WikiChunk.created_at)
            ).all()
        )

    def _active_chunk_counts(self, article_ids: list[UUID]) -> dict[UUID, int]:
        if not article_ids:
            return {}
        rows = self.session.execute(
            select(WikiChunk.article_id, func.count(WikiChunk.chunk_id))
            .where(
                WikiChunk.article_id.in_(article_ids),
                WikiChunk.status == WikiChunkStatus.ACTIVE,
            )
            .group_by(WikiChunk.article_id)
        ).all()
        return {article_id: int(count) for article_id, count in rows}

    @staticmethod
    def _article_summary(article: WikiArticle, chunk_count: int) -> WikiArticleSummaryResponse:
        return WikiArticleSummaryResponse(
            article_id=article.article_id,
            title=article.title,
            summary=article.summary,
            language=article.language,
            categories=list(article.categories or []),
            domain=article.domain,
            source=article.source,
            source_page_id=article.source_page_id,
            revision_id=article.revision_id,
            source_url=article.source_url,
            source_updated_at=article.source_updated_at,
            sync_time=article.sync_time,
            status=article.status.value,
            chunk_count=chunk_count,
        )

    @staticmethod
    def _toc(
        article: WikiArticle, chunks: list[WikiChunk], *, include_inactive: bool
    ) -> list[WikiTocEntry]:
        entries: list[WikiTocEntry] = []
        seen: set[tuple[str, ...]] = set()
        for chunk in chunks:
            if not include_inactive and chunk.status != WikiChunkStatus.ACTIVE:
                continue
            path = tuple(chunk.heading_path or [])
            if not path or path in seen:
                continue
            seen.add(path)
            entries.append(WikiTocEntry(heading_path=list(path), chunk_index=chunk.chunk_index))
        return entries

    def _task_jobs(self, task_ids: list[UUID]) -> dict[UUID, BackgroundJob]:
        if not task_ids:
            return {}
        rows = self.session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == BackgroundJobType.WIKI_SYNC,
                BackgroundJob.resource_id.in_(task_ids),
            )
            .order_by(BackgroundJob.created_at.desc())
        ).all()
        jobs: dict[UUID, BackgroundJob] = {}
        for job in rows:
            jobs.setdefault(job.resource_id, job)
        return jobs

    def _task_response(
        self, task: WikiSyncTask, job: BackgroundJob | None
    ) -> WikiSyncTaskResponse:
        errors = [
            WikiSyncErrorItem(**item)
            for item in (task.error_sample or [])
            if isinstance(item, dict)
        ]
        return WikiSyncTaskResponse(
            task_id=task.task_id,
            job_id=job.job_id if job is not None else None,
            source=task.source,
            mode=task.mode.value,
            package_version=task.package_version,
            package_uri=self._package_uri(job),
            status=task.status.value,
            total_count=task.total_count,
            processed_count=task.processed_count,
            success_count=task.success_count,
            failed_count=task.failed_count,
            error_sample=errors,
            requested_by=task.requested_by,
            retryable=task.status in RETRYABLE_STATUSES,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )

    @staticmethod
    def _package_uri(job: BackgroundJob | None) -> str | None:
        payload = job.payload if job is not None and isinstance(job.payload, dict) else {}
        uri = payload.get("package_uri")
        return str(uri) if uri else None
