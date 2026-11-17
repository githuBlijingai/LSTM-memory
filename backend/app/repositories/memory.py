from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.models import Entity, EventMemory
from app.repositories.base import BaseRepository
from app.repositories.permissions import event_candidate_predicate, visibility_predicate


class EventRepository(BaseRepository[EventMemory]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, EventMemory)

    def visible_statement(self, user: CurrentUser) -> Select:
        return select(EventMemory).where(
            visibility_predicate(user, EventMemory.owner_id, EventMemory.visibility)
        )

    def candidate_statement(self, user: CurrentUser) -> Select:
        return select(EventMemory).where(event_candidate_predicate(user))


class EntityRepository(BaseRepository[Entity]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Entity)

    def visible_statement(self, user: CurrentUser) -> Select:
        return select(Entity).where(visibility_predicate(user, Entity.owner_id, Entity.visibility))
