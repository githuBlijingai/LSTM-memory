from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, pg_enum
from app.models.enums import JobStatus, WikiArticleStatus, WikiChunkStatus, WikiSyncMode


class WikiArticle(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "wiki_article"

    article_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(64))
    source_page_id: Mapped[str] = mapped_column(String(128))
    revision_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512), index=True)
    language: Mapped[str] = mapped_column(String(16), default="zh")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(JSONB, default=list)
    domain: Mapped[str | None] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    source_updated_at: Mapped[datetime | None]
    sync_time: Mapped[datetime]
    status: Mapped[WikiArticleStatus] = mapped_column(
        pg_enum(WikiArticleStatus, "wiki_article_status"), default=WikiArticleStatus.ACTIVE
    )

    __table_args__ = (
        UniqueConstraint("source", "source_page_id", name="wiki_article_source_page"),
    )


class WikiChunk(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "wiki_chunk"

    chunk_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("wiki_article.article_id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(JSONB, default=list)
    raw_text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    familiarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    familiarity_updated_at: Mapped[datetime | None]
    last_access_time: Mapped[datetime | None]
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[WikiChunkStatus] = mapped_column(
        pg_enum(WikiChunkStatus, "wiki_chunk_status"), default=WikiChunkStatus.PENDING
    )

    __table_args__ = (
        UniqueConstraint("article_id", "chunk_index", "content_hash", name="wiki_chunk_revision"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_nonnegative"),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="importance_range"),
        CheckConstraint(
            "familiarity_score >= 0 AND familiarity_score <= 2", name="familiarity_range"
        ),
        Index("ix_wiki_chunk_article_status", "article_id", "status"),
        Index(
            "ix_wiki_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class WikiSyncTask(CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "wiki_sync_task"

    task_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source: Mapped[str] = mapped_column(String(64))
    mode: Mapped[WikiSyncMode] = mapped_column(pg_enum(WikiSyncMode, "wiki_sync_mode"))
    package_version: Mapped[str] = mapped_column(String(128))
    cursor: Mapped[str | None] = mapped_column(Text)
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), default=JobStatus.QUEUED
    )
    total_count: Mapped[int | None] = mapped_column(BigInteger)
    processed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    success_count: Mapped[int] = mapped_column(BigInteger, default=0)
    failed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    error_sample: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    requested_by: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("processed_count >= 0", name="processed_nonnegative"),
        CheckConstraint("success_count >= 0", name="success_nonnegative"),
        CheckConstraint("failed_count >= 0", name="failed_nonnegative"),
    )
