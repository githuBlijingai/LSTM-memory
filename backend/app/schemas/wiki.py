from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.models.enums import WikiSyncMode
from app.schemas.json_fields import StrictSchema, WikiSyncErrorItem
from app.schemas.memory import WikiSourceRef


class WikiChunkSummaryResponse(StrictSchema):
    chunk_id: UUID
    chunk_index: int
    heading_path: list[str]
    raw_text: str
    token_count: int
    status: str
    model_version: str | None


class WikiTocEntry(StrictSchema):
    heading_path: list[str]
    chunk_index: int


class WikiArticleSummaryResponse(StrictSchema):
    article_id: UUID
    title: str
    summary: str
    language: str
    categories: list[str]
    domain: str | None
    source: str
    source_page_id: str
    revision_id: str
    source_url: str
    source_updated_at: datetime | None
    sync_time: datetime
    status: str
    chunk_count: int


class WikiArticleDetailResponse(WikiArticleSummaryResponse):
    content: str
    toc: list[WikiTocEntry]
    chunks: list[WikiChunkSummaryResponse]


class WikiChunkDetailResponse(StrictSchema):
    article_id: UUID
    article_title: str
    chunk: WikiChunkSummaryResponse
    previous_chunk_index: int | None
    next_chunk_index: int | None
    source: WikiSourceRef


class WikiSyncTaskCreateRequest(StrictSchema):
    source: str = Field(min_length=1, max_length=64)
    mode: WikiSyncMode = WikiSyncMode.INCREMENTAL
    package_uri: str | None = Field(default=None, min_length=1, max_length=512)
    package_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_package_uri(self) -> WikiSyncTaskCreateRequest:
        if self.mode != WikiSyncMode.REEMBED and not self.package_uri:
            raise ValueError("full/incremental 同步必须提供 package_uri")
        return self


class WikiSyncTaskResponse(StrictSchema):
    task_id: UUID
    job_id: UUID | None
    source: str
    mode: str
    package_version: str
    package_uri: str | None
    status: str
    total_count: int | None
    processed_count: int
    success_count: int
    failed_count: int
    error_sample: list[WikiSyncErrorItem]
    requested_by: str
    retryable: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
