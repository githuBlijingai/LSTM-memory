from enum import StrEnum


class WorkingSessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    EXPIRED = "expired"


class Visibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    REBUILDING = "rebuilding"
    ARCHIVED = "archived"


class EventStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    FAILED = "failed"
    ARCHIVED = "archived"
    PURGED = "purged"


class SnapshotKind(StrEnum):
    UPDATE = "update"
    REPLAY = "replay"
    REBUILD = "rebuild"
    RESTORE = "restore"


class ReplayStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WikiArticleStatus(StrEnum):
    ACTIVE = "active"
    REDIRECTED = "redirected"
    DELETED = "deleted"


class WikiChunkStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class WikiSyncMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    REEMBED = "reembed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class MemoryType(StrEnum):
    EVENT = "event"
    WIKI = "wiki"


class FeedbackType(StrEnum):
    EXPOSURE = "exposure"
    CLICKED = "clicked"
    ADOPTED = "adopted"
    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class BackgroundJobType(StrEnum):
    EVENT_PIPELINE = "event_pipeline"
    REPLAY = "replay"
    STATE_REBUILD = "state_rebuild"
    WIKI_SYNC = "wiki_sync"
    DECAY = "decay"
    REEMBED = "reembed"
    PURGE = "purge"
