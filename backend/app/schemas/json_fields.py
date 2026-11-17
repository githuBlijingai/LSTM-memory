from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutboxPayload(StrictSchema):
    schema_version: Literal[1] = 1
    event_id: UUID | None = None
    entity_id: UUID | None = None
    job_id: UUID | None = None
    resource_id: UUID | None = None
    operation: str | None = Field(default=None, max_length=128)
    model_version: str | None = Field(default=None, max_length=128)
    target_state_version: int | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, max_length=64)
    package_version: str | None = Field(default=None, max_length=128)
    package_uri: str | None = Field(default=None, max_length=512)
    cursor: str | None = Field(default=None, max_length=1024)
    reason: str | None = Field(default=None, max_length=256)


class BackgroundJobPayload(StrictSchema):
    schema_version: Literal[1] = 1
    operation: str = Field(max_length=128)
    entity_id: UUID | None = None
    event_id: UUID | None = None
    model_version: str | None = Field(default=None, max_length=128)
    target_state_version: int | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, max_length=64)
    package_version: str | None = Field(default=None, max_length=128)
    package_uri: str | None = Field(default=None, max_length=512)
    cursor: str | None = Field(default=None, max_length=1024)


class WikiSyncErrorItem(StrictSchema):
    code: str = Field(max_length=64)
    message: str = Field(max_length=256)
    source_item_id: str | None = Field(default=None, max_length=128)


class WikiSyncErrorSample(StrictSchema):
    items: list[WikiSyncErrorItem] = Field(default_factory=list, max_length=20)


class MemoryQueryFilters(StrictSchema):
    entity_ids: list[UUID] = Field(default_factory=list, max_length=100)
    event_types: list[str] = Field(default_factory=list, max_length=20)
    domains: list[str] = Field(default_factory=list, max_length=20)
    language: str | None = Field(default=None, max_length=16)


class IdempotencyResponseBody(StrictSchema):
    data: JsonValue
    request_id: UUID
    next_cursor: str | None = None


class TensorShape(StrictSchema):
    dimensions: list[int] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def positive_dimensions(self) -> TensorShape:
        if any(dimension <= 0 for dimension in self.dimensions):
            raise ValueError("tensor dimensions must be positive")
        return self


def to_json_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)
