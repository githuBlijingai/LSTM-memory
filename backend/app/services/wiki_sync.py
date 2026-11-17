"""Wiki 同步执行服务（AG-04，设计 3.5）。

职责：
- 解析 WikiExtractor JSONL 数据包；
- 词条 upsert、知识块切分与批量编码；
- 增量游标、断点续传、错误样本与来源删除；
- 复用正文的批量重编码。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.logging import redact_text
from app.domain.wiki import chunk_article
from app.ml.embedding import EmbeddingProvider
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
from app.schemas.json_fields import WikiSyncErrorItem
from app.services.wiki_parser import (
    WikiArticleRecord,
    iter_package_entries,
    open_package,
)
from app.services.working_memory import estimate_tokens

logger = structlog.get_logger(__name__)

MAX_ERROR_SAMPLE = 20
MAX_ERROR_MESSAGE = 256


@dataclass(frozen=True)
class WikiSyncOutcome:
    task_id: UUID
    status: str
    processed_count: int
    success_count: int
    failed_count: int
    cursor: str | None
    skipped: bool = False


def _fatal(exc: Exception) -> bool:
    """模型/向量错误必须让整个任务重试，不能记为单条失败。"""
    if isinstance(exc, SQLAlchemyError):
        return True
    if isinstance(exc, AppError):
        return exc.code.startswith(("MODEL_", "EMBEDDING_"))
    return False


class WikiSyncService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        embedding: EmbeddingProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.embedding = embedding
        self.clock = clock

    # ---- 入口 -------------------------------------------------------------

    def process_task(self, task_id: UUID) -> WikiSyncOutcome:
        with self.session_factory() as session:
            task = session.get(WikiSyncTask, task_id, with_for_update=True)
            if task is None:
                raise NotFoundError("Wiki 同步任务不存在")
            if task.status in {
                JobStatus.SUCCEEDED,
                JobStatus.CANCELLED,
                JobStatus.DEAD_LETTER,
            }:
                return self._outcome(task, skipped=True)
            task.status = JobStatus.RUNNING
            task.started_at = task.started_at or self.clock()
            task.finished_at = None
            package_uri = self._package_uri(session, task)
            mode = task.mode
            source = task.source
            session.commit()

        if not self.embedding.model_version:
            raise AppError("MODEL_UNAVAILABLE", "Embedding 模型版本未配置", 503)

        if mode == WikiSyncMode.REEMBED:
            self._reembed(task_id)
        else:
            if not package_uri:
                raise AppError(
                    "VALIDATION_ERROR", "同步任务缺少 package_uri，请重新创建任务", 409
                )
            self._ingest(task_id, source=source, package_uri=package_uri)

        with self.session_factory() as session:
            task = session.get(WikiSyncTask, task_id)
            if task is None:
                raise NotFoundError("Wiki 同步任务不存在")
            task.status = JobStatus.SUCCEEDED
            task.finished_at = self.clock()
            session.commit()
            return self._outcome(task)

    # ---- 增量/全量同步 -----------------------------------------------------

    def _ingest(self, task_id: UUID, *, source: str, package_uri: str) -> None:
        with open_package(package_uri, max_bytes=self.settings.wiki_package_max_bytes) as view:
            with self.session_factory() as session:
                task = session.get(WikiSyncTask, task_id)
                if task is None:
                    raise NotFoundError("Wiki 同步任务不存在")
                start_after = task.cursor
                from_beginning = start_after is None
                task.total_count = view.total_count
                session.commit()

                seen: set[str] = set()
                cursor = start_after
                for entry in iter_package_entries(view, start_after=start_after):
                    cursor = entry.cursor
                    if entry.record is not None:
                        seen.add(entry.record.page_id)
                    try:
                        if entry.error is not None:
                            raise AppError("WIKI_PACKAGE_INVALID", entry.error, 422)
                        if entry.record is None:
                            raise AppError("WIKI_PACKAGE_INVALID", "数据包条目为空", 422)
                        self._sync_record(session, source=source, record=entry.record)
                    except Exception as exc:  # noqa: BLE001 - 单条失败按错误样本记录
                        if _fatal(exc):
                            raise
                        session.rollback()
                        task = session.get(WikiSyncTask, task_id)
                        if task is None:
                            raise NotFoundError("Wiki 同步任务不存在") from exc
                        task.failed_count += 1
                        task.processed_count += 1
                        task.cursor = cursor
                        source_item_id = entry.record.page_id if entry.record else None
                        self._append_error(task, exc, source_item_id)
                        self._update_progress(session, task)
                        session.commit()
                        continue
                    task = session.get(WikiSyncTask, task_id)
                    if task is None:
                        raise NotFoundError("Wiki 同步任务不存在")
                    task.success_count += 1
                    task.processed_count += 1
                    task.cursor = cursor
                    self._update_progress(session, task)
                    session.commit()

                if from_beginning and task.mode == WikiSyncMode.FULL:
                    self._mark_missing_deleted(session, source=source, seen=seen)
                    session.commit()

                task = session.get(WikiSyncTask, task_id)
                if task is not None:
                    task.total_count = task.total_count or task.processed_count
                    session.commit()

    def _sync_record(self, session: Session, *, source: str, record: WikiArticleRecord) -> None:
        now = self.clock()
        article = session.scalar(
            select(WikiArticle)
            .where(
                WikiArticle.source == source,
                WikiArticle.source_page_id == record.page_id,
            )
            .with_for_update()
        )
        common = {
            "title": record.title,
            "language": record.language,
            "source_url": record.source_url,
            "categories": record.categories,
            "revision_id": record.revision_id,
            "source_updated_at": record.source_updated_at,
            "sync_time": now,
        }
        if record.redirected:
            if article is None:
                article = WikiArticle(
                    source=source,
                    source_page_id=record.page_id,
                    content="",
                    summary="",
                    content_hash=record.content_hash,
                    status=WikiArticleStatus.REDIRECTED,
                    **common,
                )
                session.add(article)
            else:
                for key, value in common.items():
                    setattr(article, key, value)
                article.status = WikiArticleStatus.REDIRECTED
                self._set_chunks_status(session, article.article_id, WikiChunkStatus.DELETED)
            session.flush()
            return

        if article is None:
            article = WikiArticle(
                source=source,
                source_page_id=record.page_id,
                summary=record.summary,
                content=record.content,
                content_hash=record.content_hash,
                status=WikiArticleStatus.ACTIVE,
                **common,
            )
            session.add(article)
            session.flush()
            self._replace_chunks(session, article, record.content)
            return

        unchanged = (
            article.content_hash == record.content_hash
            and article.status == WikiArticleStatus.ACTIVE
        )
        for key, value in common.items():
            setattr(article, key, value)
        article.summary = record.summary
        article.content = record.content
        article.content_hash = record.content_hash
        if unchanged:
            if not self._refresh_stale_chunks(session, article):
                self._replace_chunks(session, article, record.content)
            session.flush()
            return
        article.status = WikiArticleStatus.ACTIVE
        session.flush()
        self._replace_chunks(session, article, record.content)

    # ---- 知识块 -----------------------------------------------------------

    def _drafts(self, content: str):
        return chunk_article(
            content,
            min_chars=self.settings.wiki_chunk_min_chars,
            max_chars=self.settings.wiki_chunk_max_chars,
            overlap_chars=self.settings.wiki_chunk_overlap_chars,
        )

    def _encode(self, texts: list[str]) -> list[list[float]]:
        batch = self.settings.wiki_embed_batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch):
            vectors.extend(self.embedding.embed(texts[start : start + batch]))
        if len(vectors) != len(texts):
            raise AppError("EMBEDDING_INVALID", "Embedding 数量与知识块数量不一致", 502)
        return vectors

    def _replace_chunks(self, session: Session, article: WikiArticle, content: str) -> None:
        drafts = self._drafts(content)
        if not drafts:
            raise AppError("WIKI_PACKAGE_INVALID", "词条正文无法切分为知识块", 422)
        existing = {
            (chunk.chunk_index, chunk.content_hash): chunk
            for chunk in session.scalars(
                select(WikiChunk).where(WikiChunk.article_id == article.article_id)
            ).all()
        }
        kept: set[UUID] = set()
        stale: list[tuple[WikiChunk, str]] = []
        fresh: list[tuple[int, tuple[str, ...], str, str]] = []
        for index, draft in enumerate(drafts):
            digest = _chunk_digest(draft.text)
            chunk = existing.get((index, digest))
            if chunk is None:
                fresh.append((index, draft.heading_path, draft.text, digest))
                continue
            kept.add(chunk.chunk_id)
            chunk.heading_path = list(draft.heading_path)
            chunk.raw_text = draft.text
            chunk.token_count = estimate_tokens(draft.text)
            if chunk.status != WikiChunkStatus.ACTIVE:
                chunk.status = WikiChunkStatus.ACTIVE
            if chunk.embedding is None or chunk.model_version != self.embedding.model_version:
                stale.append((chunk, draft.text))

        for chunk in existing.values():
            if chunk.chunk_id not in kept and chunk.status == WikiChunkStatus.ACTIVE:
                chunk.status = WikiChunkStatus.SUPERSEDED

        if fresh:
            vectors = self._encode([text for _, _, text, _ in fresh])
            for (index, heading_path, text, digest), vector in zip(fresh, vectors, strict=True):
                session.add(
                    WikiChunk(
                        article_id=article.article_id,
                        chunk_index=index,
                        heading_path=list(heading_path),
                        raw_text=text,
                        token_count=estimate_tokens(text),
                        embedding=vector,
                        embedding_model=self.settings.embedding_model_name,
                        model_version=self.embedding.model_version,
                        content_hash=digest,
                        status=WikiChunkStatus.ACTIVE,
                    )
                )
        if stale:
            vectors = self._encode([text for _, text in stale])
            for (chunk, _), vector in zip(stale, vectors, strict=True):
                chunk.embedding = vector
                chunk.embedding_model = self.settings.embedding_model_name
                chunk.model_version = self.embedding.model_version
        session.flush()

    def _refresh_stale_chunks(self, session: Session, article: WikiArticle) -> bool:
        chunks = list(
            session.scalars(
                select(WikiChunk).where(
                    WikiChunk.article_id == article.article_id,
                    WikiChunk.status == WikiChunkStatus.ACTIVE,
                )
            ).all()
        )
        if not chunks:
            return False
        stale = [
            chunk
            for chunk in chunks
            if chunk.embedding is None or chunk.model_version != self.embedding.model_version
        ]
        if not stale:
            return True
        vectors = self._encode([chunk.raw_text for chunk in stale])
        for chunk, vector in zip(stale, vectors, strict=True):
            chunk.embedding = vector
            chunk.embedding_model = self.settings.embedding_model_name
            chunk.model_version = self.embedding.model_version
        return True

    def _set_chunks_status(
        self, session: Session, article_id: UUID, status: WikiChunkStatus
    ) -> None:
        for chunk in session.scalars(
            select(WikiChunk).where(WikiChunk.article_id == article_id)
        ).all():
            if chunk.status != status:
                chunk.status = status

    def _mark_missing_deleted(self, session: Session, *, source: str, seen: set[str]) -> None:
        articles = session.scalars(
            select(WikiArticle).where(
                WikiArticle.source == source,
                WikiArticle.status == WikiArticleStatus.ACTIVE,
            )
        ).all()
        for article in articles:
            if article.source_page_id in seen:
                continue
            article.status = WikiArticleStatus.DELETED
            self._set_chunks_status(session, article.article_id, WikiChunkStatus.DELETED)

    # ---- 重编码 -----------------------------------------------------------

    def _reembed(self, task_id: UUID) -> None:
        model_version = self.embedding.model_version
        batch = self.settings.wiki_embed_batch_size
        with self.session_factory() as session:
            source = session.scalar(
                select(WikiSyncTask.source).where(WikiSyncTask.task_id == task_id)
            )
            if source is None:
                raise NotFoundError("Wiki 同步任务不存在")
            last_id: UUID | None = None
            while True:
                statement = (
                    select(WikiChunk)
                    .join(WikiArticle, WikiArticle.article_id == WikiChunk.article_id)
                    .where(
                        WikiArticle.source == source,
                        WikiChunk.status == WikiChunkStatus.ACTIVE,
                    )
                    .order_by(WikiChunk.chunk_id)
                    .limit(batch)
                )
                if last_id is not None:
                    statement = statement.where(WikiChunk.chunk_id > last_id)
                chunks = list(session.scalars(statement).all())
                if not chunks:
                    break
                pending = [
                    chunk
                    for chunk in chunks
                    if chunk.embedding is None or chunk.model_version != model_version
                ]
                if pending:
                    vectors = self._encode([chunk.raw_text for chunk in pending])
                    for chunk, vector in zip(pending, vectors, strict=True):
                        chunk.embedding = vector
                        chunk.embedding_model = self.settings.embedding_model_name
                        chunk.model_version = model_version
                task = session.get(WikiSyncTask, task_id)
                if task is None:
                    raise NotFoundError("Wiki 同步任务不存在")
                task.processed_count += len(chunks)
                task.success_count += len(chunks)
                self._update_progress(session, task)
                session.commit()
                last_id = chunks[-1].chunk_id

    # ---- 辅助 -------------------------------------------------------------

    def _package_uri(self, session: Session, task: WikiSyncTask) -> str | None:
        job = session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == BackgroundJobType.WIKI_SYNC,
                BackgroundJob.resource_id == task.task_id,
            )
            .order_by(BackgroundJob.created_at.desc())
            .limit(1)
        )
        payload = job.payload if job is not None and isinstance(job.payload, dict) else {}
        uri = payload.get("package_uri")
        return str(uri) if uri else None

    def _update_progress(self, session: Session, task: WikiSyncTask) -> None:
        job = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == BackgroundJobType.WIKI_SYNC,
                BackgroundJob.resource_id == task.task_id,
            )
        )
        if job is None or not task.total_count:
            return
        job.progress = min(1.0, task.processed_count / task.total_count)

    def _append_error(
        self, task: WikiSyncTask, exc: Exception, source_item_id: str | None
    ) -> None:
        items = list(task.error_sample or [])
        if len(items) >= MAX_ERROR_SAMPLE:
            return
        code = exc.code if isinstance(exc, AppError) else type(exc).__name__
        items.append(
            WikiSyncErrorItem(
                code=code[:64],
                message=redact_text(str(exc))[:MAX_ERROR_MESSAGE],
                source_item_id=source_item_id,
            ).model_dump(exclude_none=True)
        )
        task.error_sample = items

    @staticmethod
    def _outcome(task: WikiSyncTask, *, skipped: bool = False) -> WikiSyncOutcome:
        return WikiSyncOutcome(
            task_id=task.task_id,
            status=task.status.value,
            processed_count=task.processed_count,
            success_count=task.success_count,
            failed_count=task.failed_count,
            cursor=task.cursor,
            skipped=skipped,
        )


def _chunk_digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
