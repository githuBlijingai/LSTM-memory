from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


@lru_cache(maxsize=8)
def get_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=8)
def get_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(database_url), expire_on_commit=False, class_=Session)


def check_database(database_url: str, timeout_seconds: float = 2.0) -> None:
    engine = get_engine(database_url)
    with engine.connect() as connection:
        connection.execution_options(timeout=timeout_seconds).execute(text("SELECT 1"))


def get_db(settings: Settings) -> Generator[Session, None, None]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    session = get_session_factory(settings.database_url)()
    try:
        yield session
    finally:
        session.close()
