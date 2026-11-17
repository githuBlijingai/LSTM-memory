from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.models.enums import FeedbackType, Visibility
from app.schemas.json_fields import StrictSchema


class EventCreateRequest(StrictSchema):
    schema_version: Literal[1] = 1
    subject: str = Field(min_length=1, max_length=128)
    entity_type: str = Field(default="topic", min_length=1, max_length=32)
    event_type: str = Field(min_length=1, max_length=64)
    event_time: datetime
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20000)
    source: Literal["api", "import"] = "api"
    source_event_key: str | None = Field(default=None, max_length=256)
    importance: float = Field(default=0.5, ge=0, le=1)
    visibility: Visibility = Visibility.PRIVATE

    @field_validator("event_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time 必须包含时区")
        return value


class EventAcceptedResponse(StrictSchema):
    event_id: UUID
    status: str
    job_id: UUID


class EventSummaryResponse(StrictSchema):
    event_id: UUID
    subject: str
    event_type: str
    title: str
    event_time: datetime
    importance: float
    status: str
    visibility: Visibility
    created_at: datetime


class EventDetailResponse(EventSummaryResponse):
    content: str | None
    entity_id: UUID
    decay_score: float
    familiarity_score: float
    source: str
    supersedes_event_id: UUID | None
    sequence_no: int | None
    event_model_version: str | None
    updated_at: datetime


class EventCorrectionRequest(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=20000)
    event_time: datetime | None = None
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("event_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("event_time 必须包含时区")
        return value


class EventCorrectionResponse(StrictSchema):
    new_event_id: UUID
    status: str
    rebuild_job_id: UUID | None


class EventDeleteRequest(StrictSchema):
    mode: Literal["archive", "purge"] = "archive"
    reason: str = Field(min_length=1, max_length=500)


class EventRestoreRequest(StrictSchema):
    reason: str = Field(min_length=1, max_length=500)


class EventDeleteResponse(StrictSchema):
    event_id: UUID
    status: str
    purge_after: datetime | None
    rebuild_job_id: UUID | None


class EventRestoreResponse(StrictSchema):
    event_id: UUID
    status: str
    rebuild_job_id: UUID | None


class SearchFiltersRequest(StrictSchema):
    subject: str | None = Field(default=None, max_length=128)
    entity_ids: list[UUID] = Field(default_factory=list, max_length=100)
    event_types: list[str] = Field(default_factory=list, max_length=20)
    start_time: datetime | None = None
    end_time: datetime | None = None
    wiki_categories: list[str] = Field(default_factory=list, max_length=20)
    wiki_languages: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def ordered_time_range(self) -> SearchFiltersRequest:
        if self.start_time and self.end_time and self.start_time > self.end_time:
            raise ValueError("start_time 不能晚于 end_time")
        return self


class SearchRequest(StrictSchema):
    query: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None
    memory_type: Literal["all", "event", "wiki"] = "all"
    filters: SearchFiltersRequest = Field(default_factory=SearchFiltersRequest)
    top_n: int = Field(default=10, ge=1, le=50)
    enable_familiarity: bool = True
    include_score_breakdown: bool = True


class SearchResultBase(StrictSchema):
    memory_id: UUID
    final_score: float
    feedback_state: str | None = None
    score_breakdown: dict[str, float] | None = None
    model_version: str | None = None


class EventSearchResult(SearchResultBase):
    memory_type: Literal["event"] = "event"
    title: str
    content: str
    event_time: datetime


class WikiSourceRef(StrictSchema):
    name: str
    url: str
    page_id: str
    revision_id: str


class WikiSearchResult(SearchResultBase):
    memory_type: Literal["wiki"] = "wiki"
    article_id: UUID
    title: str
    content: str
    chunk_index: int
    heading_path: list[str]
    source: WikiSourceRef


class SearchResponse(StrictSchema):
    query_id: UUID
    intent: Literal["event", "wiki", "mixed"]
    results: list[EventSearchResult | WikiSearchResult]


class FeedbackRequest(StrictSchema):
    query_id: UUID
    session_id: UUID | None = None
    memory_type: Literal["event", "wiki"]
    memory_id: UUID
    feedback_type: FeedbackType


class FeedbackResponse(StrictSchema):
    accepted: bool
    counted: bool
    not_counted_reason: str | None
    familiarity_score: float
    replay_job_created: bool


class WorkingSessionCreateRequest(StrictSchema):
    token_budget: int | None = Field(default=None, ge=1000, le=32000)


class WorkingSessionResponse(StrictSchema):
    session_id: UUID
    actor_id: str
    status: str
    summary: str
    summary_version: str | None
    token_budget: int
    token_count: int
    last_activity_at: datetime
    expires_at: datetime | None


class WorkingItemResponse(StrictSchema):
    item_id: UUID
    sequence_no: int
    role: str
    content: str
    token_count: int
    importance: float
    explicit_keep: bool
    consolidated_event_id: UUID | None
    created_at: datetime


class WorkingSessionDetailResponse(WorkingSessionResponse):
    items: list[WorkingItemResponse]


class WorkingItemAppendRequest(StrictSchema):
    role: Literal["user", "assistant", "system", "tool"]
    content: str = Field(min_length=1, max_length=20000)
    importance: float = Field(default=0.5, ge=0, le=1)
    explicit_keep: bool = False


class WorkingItemAppendResponse(StrictSchema):
    session_id: UUID
    item_id: UUID
    token_count: int
    session_token_count: int
    summary_triggered: bool
    summary_version: str | None = None


class ConsolidateEventRef(StrictSchema):
    event_id: UUID
    status: str


class WorkingConsolidateResponse(StrictSchema):
    session_id: UUID
    events: list[ConsolidateEventRef]


class WorkingCloseResponse(StrictSchema):
    session_id: UUID
    status: str
    expires_at: datetime | None


class EntitySummaryResponse(StrictSchema):
    entity_id: UUID
    entity_name: str
    entity_type: str
    status: str
    visibility: Visibility
    readable_summary: str
    state_version: int
    model_version: str | None
    last_sequence_no: int
    last_update_time: datetime | None


class EntityDetailResponse(EntitySummaryResponse):
    owner_id: str
    source_event_ids: list[UUID]
    summary_version: str | None
    summary_updated_at: datetime | None
    last_event_id: UUID | None
    last_event_time: datetime | None


class EntityStateResponse(StrictSchema):
    entity_id: UUID
    status: str
    state_version: int
    model_version: str | None
    last_event_id: UUID | None
    last_sequence_no: int
    last_update_time: datetime | None
    latest_snapshot_at: datetime | None
    pending_replay_count: int
    active_rebuild_job_id: UUID | None


class ReplayLogResponse(StrictSchema):
    replay_id: UUID
    event_id: UUID
    reason: str
    before_state_version: int
    after_state_version: int | None
    model_version: str
    status: str
    attempt_count: int
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class EntityRebuildResponse(StrictSchema):
    entity_id: UUID
    job_id: UUID
    status: str


class JobResponse(StrictSchema):
    job_id: UUID
    job_type: str
    resource_type: str
    resource_id: UUID
    status: str
    progress: float
    attempt_count: int
    last_error: str | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DashboardSummaryResponse(StrictSchema):
    events: dict[str, int]
    wiki: dict[str, int | datetime | None]
    jobs: dict[str, int]
    outbox: dict[str, int]
    alerts: dict[str, int]


class FamiliarityUpdateRequest(StrictSchema):
    score: float = Field(ge=0, le=2)
    reason: str = Field(min_length=1, max_length=500)


class FamiliarityUpdateResponse(StrictSchema):
    memory_type: Literal["event", "wiki"]
    memory_id: UUID
    familiarity_score: float
    updated_at: datetime
