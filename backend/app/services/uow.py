from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditLog, BackgroundJob, IdempotencyRecord, OutboxEvent
from app.repositories.base import BaseRepository
from app.repositories.memory import EntityRepository, EventRepository


class UnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session] | Callable[[], Session]) -> None:
        self.session_factory = session_factory
        self.session: Session

    def __enter__(self) -> UnitOfWork:
        self.session = self.session_factory()
        self.events = EventRepository(self.session)
        self.entities = EntityRepository(self.session)
        self.outbox = BaseRepository(self.session, OutboxEvent)
        self.jobs = BaseRepository(self.session, BackgroundJob)
        self.idempotency = BaseRepository(self.session, IdempotencyRecord)
        self.audits = BaseRepository(self.session, AuditLog)
        return self

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollback()
        self.session.close()
