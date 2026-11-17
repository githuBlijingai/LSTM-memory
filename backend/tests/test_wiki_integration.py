from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from alembic import command
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import CurrentUser, Role
from app.models import (
    BackgroundJob,
    JobStatus,
    WikiArticle,
    WikiArticleStatus,
    WikiChunk,
    WikiChunkStatus,
    WikiSyncMode,
    WikiSyncTask,
)
from app.schemas.wiki import WikiSyncTaskCreateRequest
from app.services.wiki_reads import WikiReadService
from app.services.wiki_sync import WikiSyncService

pytestmark = pytest.mark.integration

if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests",
        allow_module_level=True,
    )

PostgresContainer = pytest.importorskip("testcontainers.postgres").PostgresContainer


def _postgres_container():
    return PostgresContainer("pgvector/pgvector:pg16")


def _database_url(postgres) -> str:
    # get_connection_url 在该版本固定使用容器端口 5432，会误连宿主机 PostgreSQL，
    # 因此显式使用 Docker 映射出来的宿主机端口。
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    assert port != 5432, "测试容器应使用随机宿主机端口，避免连到宿主机数据库"
    return (
        f"postgresql+psycopg://{postgres.username}:{postgres.password}"
        f"@{host}:{port}/{postgres.dbname}"
    )

ARTICLE_ONE = """\
== 概述 ==

向量数据库用于存储和检索高维向量，是分层记忆系统的检索基础。

== 架构 ==

系统分为事件层、知识层与工作记忆层，各层通过统一的检索接口协作。
"""

ARTICLE_TWO = """\
== 简介 ==

分层记忆系统把短期事件与长期知识分开存储，避免上下文无限增长。
"""


class FakeEmbedding:
    def __init__(self, model_version: str = "embed-v1") -> None:
        self.model_version = model_version
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vectors = []
        for item in texts:
            digest = hashlib.sha256(item.encode("utf-8")).digest()
            raw = [float(byte) / 255.0 + 0.01 for byte in digest]
            raw = (raw * (1024 // len(raw) + 1))[:1024]
            norm = sum(value * value for value in raw) ** 0.5
            vectors.append([value / norm for value in raw])
        return vectors


def _migration_config(database_url: str) -> Config:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _line(page_id: str, title: str, content: str) -> str:
    return json.dumps(
        {
            "id": page_id,
            "revid": f"rev-{page_id}",
            "title": title,
            "url": f"https://wiki.example/{page_id}",
            "text": content,
            "lang": "zh",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        ensure_ascii=False,
    )


def _write_dump(path: Path, entries: list[tuple[str, str, str]]) -> None:
    path.write_text("\n".join(_line(*entry) for entry in entries), encoding="utf-8")


def test_wiki_sync_read_delete_and_reembed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _postgres_container() as postgres:
        database_url = _database_url(postgres)
        monkeypatch.setenv("DATABASE_URL", database_url)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        command.upgrade(_migration_config(database_url), "head")

        settings = Settings(_env_file=None, database_url=database_url)
        operator = CurrentUser(subject="ops", roles=frozenset({Role.OPERATOR}))
        viewer = CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
        embedding = FakeEmbedding()
        factory = _session_factory(engine)

        dump = tmp_path / "dump.jsonl"
        _write_dump(dump, [("1", "向量数据库", ARTICLE_ONE), ("2", "分层记忆", ARTICLE_TWO)])
        with factory() as session:
            read_service = WikiReadService(session, settings)
            task, job_id = read_service.create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.FULL,
                    package_uri=str(dump),
                    package_version="dump-1",
                ),
            )
            session.commit()
            task_id = task.task_id
            assert task.status == "queued"
            assert task.package_uri == str(dump)

        outcome = WikiSyncService(factory, settings, embedding).process_task(task_id)
        assert outcome.status == "succeeded"
        assert outcome.processed_count == 2
        assert outcome.success_count == 2
        assert outcome.failed_count == 0

        with factory() as session:
            articles = session.scalars(select(WikiArticle).order_by(WikiArticle.title)).all()
            assert [article.title for article in articles] == ["分层记忆", "向量数据库"]
            assert all(article.status == WikiArticleStatus.ACTIVE for article in articles)
            chunks = session.scalars(
                select(WikiChunk)
                .where(WikiChunk.status == WikiChunkStatus.ACTIVE)
                .order_by(WikiChunk.chunk_index)
            ).all()
            assert len(chunks) >= 3
            assert all(chunk.embedding is not None for chunk in chunks)
            assert all(chunk.token_count > 0 for chunk in chunks)
            assert all(chunk.model_version == "embed-v1" for chunk in chunks)

            page = WikiReadService(session, settings).list_articles(
                viewer,
                query="向量",
                category=None,
                language=None,
                status=None,
                cursor=None,
                limit=20,
            )
            assert [item.title for item in page.items] == ["向量数据库"]
            assert page.items[0].chunk_count >= 1

            article_id = page.items[0].article_id
            detail = WikiReadService(session, settings).get_article(viewer, article_id)
            assert detail.chunks
            assert detail.toc
            first_chunk = detail.chunks[0]
            chunk_detail = WikiReadService(session, settings).get_chunk(
                viewer, article_id, first_chunk.chunk_index
            )
            assert chunk_detail.article_id == article_id
            assert chunk_detail.source.page_id == "1"

        # 相同内容再次全量同步：不重复建块、不重复编码
        embedding.calls = 0
        with factory() as session:
            task, _ = WikiReadService(session, settings).create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.FULL,
                    package_uri=str(dump),
                    package_version="dump-1",
                ),
            )
            session.commit()
            task_id = task.task_id
        WikiSyncService(factory, settings, embedding).process_task(task_id)
        with factory() as session:
            total_chunks = len(session.scalars(select(WikiChunk)).all())
            assert total_chunks == len(chunks)

        # 内容更新：旧块标记 superseded，新块 active
        updated = ARTICLE_ONE.replace("检索基础", "检索与重排基础")
        _write_dump(dump, [("1", "向量数据库", updated), ("2", "分层记忆", ARTICLE_TWO)])
        with factory() as session:
            task, _ = WikiReadService(session, settings).create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.FULL,
                    package_uri=str(dump),
                    package_version="dump-2",
                ),
            )
            session.commit()
            task_id = task.task_id
        WikiSyncService(factory, settings, embedding).process_task(task_id)
        with factory() as session:
            article = session.scalar(
                select(WikiArticle).where(WikiArticle.source_page_id == "1")
            )
            statuses = {
                chunk.status
                for chunk in session.scalars(
                    select(WikiChunk).where(WikiChunk.article_id == article.article_id)
                )
            }
            assert WikiChunkStatus.SUPERSEDED in statuses
            assert WikiChunkStatus.ACTIVE in statuses

        # 全量同步缺失词条：来源删除
        _write_dump(dump, [("2", "分层记忆", ARTICLE_TWO)])
        with factory() as session:
            task, _ = WikiReadService(session, settings).create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.FULL,
                    package_uri=str(dump),
                    package_version="dump-3",
                ),
            )
            session.commit()
            task_id = task.task_id
        WikiSyncService(factory, settings, embedding).process_task(task_id)
        with factory() as session:
            removed = session.scalar(select(WikiArticle).where(WikiArticle.source_page_id == "1"))
            assert removed.status == WikiArticleStatus.DELETED
            assert all(
                chunk.status == WikiChunkStatus.DELETED
                for chunk in session.scalars(
                    select(WikiChunk).where(WikiChunk.article_id == removed.article_id)
                )
            )
            with pytest.raises(AppError):
                WikiReadService(session, settings).get_article(viewer, removed.article_id)

        # 模型版本变化：完整重编码
        upgraded = FakeEmbedding(model_version="embed-v2")
        with factory() as session:
            task, _ = WikiReadService(session, settings).create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.REEMBED,
                    package_version="embed-v2",
                ),
            )
            session.commit()
            task_id = task.task_id
        WikiSyncService(factory, settings, upgraded).process_task(task_id)
        with factory() as session:
            active = session.scalars(
                select(WikiChunk).where(WikiChunk.status == WikiChunkStatus.ACTIVE)
            ).all()
            assert active
            assert all(chunk.model_version == "embed-v2" for chunk in active)
            task = session.get(WikiSyncTask, task_id)
            assert task.status == JobStatus.SUCCEEDED

        # 失败任务重试：恢复排队并复用原任务
        with factory() as session:
            task = session.get(WikiSyncTask, task_id)
            task.status = JobStatus.FAILED
            session.commit()
            response, retry_job_id = WikiReadService(session, settings).retry_sync_task(
                operator, task_id
            )
            assert response.status == "queued"
            assert response.retryable is False
            job = session.get(BackgroundJob, retry_job_id)
            assert job.status == JobStatus.QUEUED


def _session_factory(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def test_sync_task_conflict_and_role_guard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _postgres_container() as postgres:
        database_url = _database_url(postgres)
        monkeypatch.setenv("DATABASE_URL", database_url)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        command.upgrade(_migration_config(database_url), "head")
        settings = Settings(_env_file=None, database_url=database_url)
        operator = CurrentUser(subject="ops", roles=frozenset({Role.OPERATOR}))
        viewer = CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
        factory = _session_factory(engine)

        dump = tmp_path / "dump.jsonl"
        _write_dump(dump, [("1", "词条", ARTICLE_TWO)])
        with factory() as session:
            service = WikiReadService(session, settings)
            service.create_sync_task(
                operator,
                WikiSyncTaskCreateRequest(
                    source="local",
                    mode=WikiSyncMode.FULL,
                    package_uri=str(dump),
                    package_version="dump-1",
                ),
            )
            session.commit()

            with pytest.raises(AppError) as conflict:
                service.create_sync_task(
                    operator,
                    WikiSyncTaskCreateRequest(
                        source="local",
                        mode=WikiSyncMode.INCREMENTAL,
                        package_uri=str(dump),
                        package_version="dump-2",
                    ),
                )
            assert conflict.value.code == "TASK_CONFLICT"

            page = service.list_sync_tasks(
                viewer, source=None, mode=None, status=None, cursor=None, limit=20
            )
            assert len(page.items) == 1
            detail = service.get_sync_task(viewer, page.items[0].task_id)
            assert detail.package_uri == str(dump)
