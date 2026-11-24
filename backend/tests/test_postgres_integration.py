from __future__ import annotations

import hashlib
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.errors import AppError, IdempotencyConflictError
from app.core.security import CurrentUser, Role
from app.ml.event_encoder import LSTMEventEncoder
from app.ml.query_encoder import QueryAdapter
from app.ml.registry import ModelBundle, ModelPackage
from app.models import (
    BackgroundJob,
    BackgroundJobType,
    Entity,
    EventMemory,
    EventStatus,
    FeedbackType,
    OutboxEvent,
    OutboxStatus,
    RetrievalHitGain,
    Visibility,
    WorkingItem,
    WorkingSession,
    WorkingSessionStatus,
)
from app.repositories.memory import EventRepository
from app.schemas.json_fields import IdempotencyResponseBody
from app.schemas.memory import (
    EventCorrectionRequest,
    EventCreateRequest,
    EventDeleteRequest,
    FeedbackRequest,
    SearchRequest,
)
from app.services.audit import AuditLogService
from app.services.event_memory import EventPipelineService
from app.services.feedback import FeedbackService
from app.services.idempotency import IdempotencyService, hash_request
from app.services.memory_reads import MemoryReadService
from app.services.memory_search import MemorySearchService
from app.services.memory_writes import EventCommandService
from app.services.outbox import OutboxService
from app.services.retrieval_reinforcement import RetrievalReinforcementService
from app.services.uow import UnitOfWork

pytestmark = pytest.mark.integration

if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
    pytest.skip(
        "set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests",
        allow_module_level=True,
    )

PostgresContainer = pytest.importorskip("testcontainers.postgres").PostgresContainer


def migration_config(database_url: str) -> Config:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_migrations_repositories_permissions_idempotency_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        database_url = "postgresql+psycopg://" + raw_url.split("://", 1)[1]
        monkeypatch.setenv("DATABASE_URL", database_url)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        config = migration_config(database_url)
        command.upgrade(config, "head")
        assert "entity" in inspect(engine).get_table_names()
        command.downgrade(config, "base")
        assert "entity" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")

        now = datetime(2026, 1, 1, tzinfo=UTC)
        with Session(engine) as session:
            entity = Entity(
                entity_name="Project Alpha",
                entity_type="project",
                owner_id="alice",
                visibility=Visibility.PRIVATE,
            )
            session.add(entity)
            session.flush()
            events = [
                EventMemory(
                    entity_id=entity.entity_id,
                    subject="Project Alpha",
                    event_type="decision",
                    event_time=now,
                    title="Alice private",
                    raw_content="Alice private",
                    embedding=[1.0] + [0.0] * 255,
                    source="api",
                    content_hash="a" * 64,
                    status=EventStatus.ACTIVE,
                    visibility=Visibility.PRIVATE,
                    owner_id="alice",
                    created_at=now,
                ),
                EventMemory(
                    entity_id=entity.entity_id,
                    subject="Project Alpha",
                    event_type="decision",
                    event_time=now,
                    title="Bob private",
                    raw_content="Bob private",
                    embedding=[1.0] + [0.0] * 255,
                    source="api",
                    content_hash="b" * 64,
                    status=EventStatus.ACTIVE,
                    visibility=Visibility.PRIVATE,
                    owner_id="bob",
                    created_at=now + timedelta(seconds=1),
                ),
                EventMemory(
                    entity_id=entity.entity_id,
                    subject="Project Alpha",
                    event_type="decision",
                    event_time=now,
                    title="Bob shared",
                    raw_content="Bob shared",
                    embedding=[1.0] + [0.0] * 255,
                    source="api",
                    content_hash="c" * 64,
                    status=EventStatus.ACTIVE,
                    visibility=Visibility.SHARED,
                    owner_id="bob",
                    created_at=now + timedelta(seconds=2),
                ),
                EventMemory(
                    entity_id=entity.entity_id,
                    subject="Project Alpha",
                    event_type="decision",
                    event_time=now,
                    title="Alice archived",
                    raw_content="Alice archived",
                    embedding=[1.0] + [0.0] * 255,
                    source="api",
                    content_hash="d" * 64,
                    status=EventStatus.ARCHIVED,
                    visibility=Visibility.PRIVATE,
                    owner_id="alice",
                    created_at=now + timedelta(seconds=3),
                ),
            ]
            session.add_all(events)
            session.flush()

            alice = CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
            repository = EventRepository(session)
            candidates = session.scalars(repository.candidate_statement(alice)).all()
            assert {event.title for event in candidates} == {"Alice private", "Bob shared"}

            first_page = repository.list_page(
                statement=repository.visible_statement(alice), limit=2
            )
            assert first_page.next_cursor
            second_page = repository.list_page(
                statement=repository.visible_statement(alice),
                cursor=first_page.next_cursor,
                limit=2,
            )
            assert len(first_page.items) == 2
            assert len(second_page.items) == 1
            assert second_page.next_cursor is None
            assert {event.title for event in first_page.items + second_page.items} == {
                "Alice private",
                "Bob shared",
                "Alice archived",
            }

            # 事件列表支持 owner_id 过滤与双向时间排序（设计 6.3）。
            read_settings = Settings(_env_file=None, environment="test", redis_url=None)
            reads = MemoryReadService(session, read_settings)
            bob_only = reads.list_events(
                alice,
                subject=None,
                entity_id=None,
                event_type=None,
                start_time=None,
                end_time=None,
                status=None,
                owner_id="bob",
                cursor=None,
                limit=20,
            )
            assert [item.title for item in bob_only.items] == ["Bob shared"]

            # 概览事件计数必须与事件列表同口径（仅统计调用者可见事件）。
            summary = reads.dashboard_summary(alice)
            assert summary.events.get("active") == 2
            assert summary.events.get("archived") == 1

            ascending_first = reads.list_events(
                alice,
                subject=None,
                entity_id=None,
                event_type=None,
                start_time=None,
                end_time=None,
                status=None,
                cursor=None,
                limit=2,
                descending=False,
            )
            assert ascending_first.next_cursor
            ascending_second = reads.list_events(
                alice,
                subject=None,
                entity_id=None,
                event_type=None,
                start_time=None,
                end_time=None,
                status=None,
                cursor=ascending_first.next_cursor,
                limit=2,
                descending=False,
            )
            assert ascending_second.next_cursor is None
            ascending_items = ascending_first.items + ascending_second.items
            assert len({item.event_id for item in ascending_items}) == 3
            assert {item.title for item in ascending_items} == {
                "Alice private",
                "Bob shared",
                "Alice archived",
            }

            request_hash = hash_request("POST", "/events", {"title": "Alpha"})
            idempotency = IdempotencyService(session)
            first = idempotency.begin(
                actor_id="alice",
                key="integration-key",
                request_path="/events",
                request_hash=request_hash,
            )
            idempotency.complete(
                first.record,
                response_status=201,
                response_body=IdempotencyResponseBody(
                    data={"event_id": str(events[0].event_id)}, request_id=uuid4()
                ),
                resource_id=events[0].event_id,
            )
            replay = idempotency.begin(
                actor_id="alice",
                key="integration-key",
                request_path="/events",
                request_hash=request_hash,
            )
            assert replay.replay
            with pytest.raises(IdempotencyConflictError):
                idempotency.begin(
                    actor_id="alice",
                    key="integration-key",
                    request_path="/events",
                    request_hash="different",
                )

            audit = AuditLogService(session).record(
                actor_id="alice",
                action="event.correct",
                resource_type="event",
                resource_id=events[0].event_id,
                reason="integration test",
                before={"title": "before", "api_key": "secret"},
                after={"title": "after"},
            )
            assert audit.audit_id is not None
            assert audit.before_json["api_key"] == "[REDACTED]"

            outbox = OutboxService(session)
            dead = OutboxEvent(
                aggregate_type="event",
                aggregate_id=events[0].event_id,
                event_type="memory.event.created",
                payload={"event_id": str(events[0].event_id)},
                status=OutboxStatus.DEAD_LETTER,
                attempt_count=5,
                last_error="boom",
                available_at=datetime.now(UTC),
            )
            healthy = OutboxEvent(
                aggregate_type="event",
                aggregate_id=events[1].event_id,
                event_type="memory.event.created",
                payload={},
                status=OutboxStatus.PENDING,
                available_at=datetime.now(UTC),
            )
            session.add_all([dead, healthy])
            session.flush()

            page = outbox.list_dead_letters(limit=10)
            assert [item.outbox_id for item in page.items] == [dead.outbox_id]

            replayed = outbox.replay_dead_letter(
                dead.outbox_id,
                actor_id="alice",
                reason="integration test",
            )
            assert replayed.status == OutboxStatus.PENDING
            assert replayed.attempt_count == 0
            assert replayed.last_error is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.status == OutboxStatus.DEAD_LETTER)
                )
                == 0
            )
            with pytest.raises(AppError) as conflict:
                outbox.replay_dead_letter(
                    healthy.outbox_id,
                    actor_id="alice",
                    reason="integration test",
                )
            assert conflict.value.status_code == 409
            session.commit()


class DeterministicEmbedding:
    """测试专用确定性 Embedding：只验证编排，不进入生产路径。"""

    model_version = "test-embedding-v1"

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for item in texts:
            vector = [0.0] * self.dimension
            for token in item.lower().split():
                index = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % self.dimension
                vector[index] += 1.0
            norm = math.sqrt(math.fsum(value * value for value in vector))
            if norm == 0:
                vector[0] = 1.0
            else:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors


def _test_model_bundle(dimension: int = 256) -> ModelBundle:
    """未训练 LSTM 仅用于集成测试；生产路径仍强制加载模型包权重。"""
    encoder = LSTMEventEncoder(
        model_version="test-lstm-v1",
        base_dim=1024,
        input_dim=dimension,
        hidden_dim=dimension,
        allow_untrained=True,
    )
    package = ModelPackage(
        uri="test://untrained",
        model_version="test-lstm-v1",
        base_embedding_dim=1024,
        input_dim=dimension,
        hidden_dim=dimension,
        num_layers=1,
        weights_path=Path("unused"),
        weights_sha256="0" * 64,
        manifest={},
        config={},
    )
    return ModelBundle(
        embedding=DeterministicEmbedding(),
        reranker=None,
        event_encoder=encoder,
        query_encoder=QueryAdapter(encoder),
        embedding_model_version="test-embedding-v1",
        event_model_version="test-lstm-v1",
        package=package,
    )


def test_event_search_feedback_replay_rebuild_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖事件写入 → 检索 → 有效反馈 → 重放 → 重建的完整链路。"""
    from app.tasks import rebuild as rebuild_task
    from app.tasks import replay as replay_task

    models = _test_model_bundle()
    settings = Settings(
        _env_file=None,
        environment="test",
        redis_url=None,
        reranker_enabled=False,
        base_embedding_dim=1024,
        lstm_input_dim=256,
        lstm_hidden_dim=256,
        search_min_similarity=-1.0,
        replay_threshold=0.05,
    )

    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        database_url = "postgresql+psycopg://" + raw_url.split("://", 1)[1]
        monkeypatch.setenv("DATABASE_URL", database_url)
        settings = settings.model_copy(update={"database_url": database_url})
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        command.upgrade(migration_config(database_url), "head")

        factory = sessionmaker(bind=engine)
        alice = CurrentUser(subject="alice", roles=frozenset({Role.VIEWER}))
        operator = CurrentUser(subject="alice", roles=frozenset({Role.OPERATOR}))
        admin = CurrentUser(subject="admin", roles=frozenset({Role.ADMIN}))

        with UnitOfWork(factory) as uow:
            outcome = EventCommandService(uow.session, settings).create_event(
                alice,
                EventCreateRequest(
                    subject="项目 Alpha",
                    entity_type="user",
                    event_type="decision",
                    event_time=datetime(2026, 11, 12, 8, 0, tzinfo=UTC),
                    title="确定检索候选阈值",
                    content="项目 Alpha 决定将首版向量候选最低余弦相似度设为 0.15。",
                    importance=0.8,
                ),
            )
            event_id = outcome.event.event_id
            uow.commit()

        pipeline = EventPipelineService(factory, settings, models).process_event(event_id)
        assert pipeline.status == "processed"
        assert pipeline.sequence_no == 1

        with UnitOfWork(factory) as uow:
            search = MemorySearchService(uow.session, settings, models).search(
                user=alice, request=SearchRequest(query="项目 Alpha 的检索阈值", top_n=10)
            )
            uow.commit()
        assert search.query_id is not None
        assert any(result.memory_id == event_id for result in search.results)

        with UnitOfWork(factory) as uow:
            feedback = FeedbackService(uow.session, settings).submit(
                user=alice,
                request=FeedbackRequest(
                    query_id=search.query_id,
                    memory_type="event",
                    memory_id=event_id,
                    feedback_type=FeedbackType.HELPFUL,
                ),
            )
            uow.commit()
        assert feedback.response.counted
        assert feedback.response.familiarity_score == pytest.approx(0.08)
        assert feedback.replay_event_id == event_id
        before_version = feedback.replay_before_state_version
        assert before_version == 1

        registry = SimpleNamespace(get_bundle=lambda: models)
        monkeypatch.setattr(replay_task, "get_settings", lambda: settings)
        monkeypatch.setattr(replay_task, "get_model_registry", lambda _settings: registry)
        replay_result = replay_task.state_replay.apply(args=[str(event_id), before_version]).get()
        assert replay_result["status"] == "succeeded"
        assert replay_result["after_state_version"] == before_version + 1

        with UnitOfWork(factory) as uow:
            event = uow.session.get(EventMemory, event_id)
            assert event is not None
            entity_id = event.entity_id
            rebuild = MemoryReadService(uow.session, settings).request_rebuild(admin, entity_id)
            uow.commit()

        monkeypatch.setattr(rebuild_task, "get_settings", lambda: settings)
        monkeypatch.setattr(rebuild_task, "get_model_registry", lambda _settings: registry)
        rebuild_result = rebuild_task.state_rebuild.apply(
            args=[str(entity_id), str(rebuild.job_id)]
        ).get()
        assert rebuild_result["status"] == "succeeded"
        assert rebuild_result["state_version"] >= 2
        assert rebuild_result["last_sequence_no"] == 1

        with UnitOfWork(factory) as uow:
            state = MemoryReadService(uow.session, settings).get_entity_state(operator, entity_id)
            assert state.state_version >= 2
            assert state.pending_replay_count == 0

            entity_events = MemoryReadService(uow.session, settings).list_entity_events(
                alice,
                entity_id,
                event_type=None,
                start_time=None,
                end_time=None,
                cursor=None,
                limit=20,
            )
            assert [item.event_id for item in entity_events.items] == [event_id]

            # 仪表盘按设计 8.2 对 viewer 开放。
            summary = MemoryReadService(uow.session, settings).dashboard_summary(alice)
            assert summary.events
            assert "last_sync_at" in summary.wiki
            assert "last_sync_time" not in summary.wiki

        # 设计 8.2：纠偏、归档与清除都属于 admin 操作，服务层不能只靠路由拦截。
        with UnitOfWork(factory) as uow:
            service = EventCommandService(uow.session, settings)
            with pytest.raises(AppError) as correct_denied:
                service.correct_event(
                    alice,
                    event_id,
                    EventCorrectionRequest(content="viewer 不应能纠偏", reason="权限回归"),
                )
            assert correct_denied.value.code == "FORBIDDEN"
            with pytest.raises(AppError) as delete_denied:
                service.delete_event(
                    alice, event_id, EventDeleteRequest(reason="viewer 不应能归档")
                )
            assert delete_denied.value.code == "FORBIDDEN"

            corrected_id = service.correct_event(
                admin,
                event_id,
                EventCorrectionRequest(content="管理员纠偏后的内容", reason="权限回归"),
            ).event.event_id
            pipeline_job = uow.session.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.job_type == BackgroundJobType.EVENT_PIPELINE,
                    BackgroundJob.resource_id == corrected_id,
                )
            )
            pipeline_outbox = uow.session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == corrected_id,
                    OutboxEvent.event_type == "memory.event.created",
                )
            )
            assert pipeline_job is not None, "纠偏新事件必须创建 EVENT_PIPELINE 任务"
            assert pipeline_outbox is not None, "纠偏新事件必须写入 Outbox"
            uow.commit()

        with UnitOfWork(factory) as uow:
            service = EventCommandService(uow.session, settings)
            archived = service.delete_event(
                admin, corrected_id, EventDeleteRequest(reason="管理员归档")
            )
            assert archived.event.status is EventStatus.ARCHIVED
            uow.commit()

        # 工作记忆 24 小时过期清理：只删除已过期会话的原始条目，未过期会话保持不变（设计 3.2）。
        from app.tasks.maintenance import run_maintenance

        maintenance_now = datetime(2026, 1, 2, tzinfo=UTC)
        with UnitOfWork(factory) as uow:
            expired_session = WorkingSession(
                actor_id="alice",
                status=WorkingSessionStatus.CLOSED,
                summary="过期会话摘要",
                last_activity_at=maintenance_now - timedelta(days=2),
                expires_at=maintenance_now - timedelta(hours=1),
            )
            fresh_session = WorkingSession(
                actor_id="alice",
                status=WorkingSessionStatus.CLOSED,
                summary="未过期会话摘要",
                last_activity_at=maintenance_now,
                expires_at=maintenance_now + timedelta(hours=23),
            )
            active_session = WorkingSession(
                actor_id="alice",
                status=WorkingSessionStatus.ACTIVE,
                last_activity_at=maintenance_now,
                expires_at=None,
            )
            uow.session.add_all([expired_session, fresh_session, active_session])
            uow.session.flush()
            session_ids = [
                expired_session.session_id,
                fresh_session.session_id,
                active_session.session_id,
            ]
            for session_id in session_ids:
                uow.session.add(
                    WorkingItem(
                        session_id=session_id,
                        sequence_no=1,
                        role="user",
                        content=f"item-{session_id}",
                        token_count=1,
                    )
                )
            uow.commit()
            expired_id, fresh_id, active_id = session_ids

        stats = run_maintenance(factory, settings, maintenance_now)
        assert stats["working_sessions_expired"] == 1
        assert stats["working_items_cleared"] == 1

        with Session(engine) as check:
            counts = {
                session_id: check.scalar(
                    select(func.count())
                    .select_from(WorkingItem)
                    .where(WorkingItem.session_id == session_id)
                )
                for session_id in (expired_id, fresh_id, active_id)
            }
            assert counts == {expired_id: 0, fresh_id: 1, active_id: 1}
            expired_row = check.get(WorkingSession, expired_id)
            assert expired_row is not None
            assert expired_row.status == WorkingSessionStatus.EXPIRED
            assert expired_row.summary == "过期会话摘要"
            assert check.get(WorkingSession, fresh_id).status == WorkingSessionStatus.CLOSED
            assert check.get(WorkingSession, active_id).status == WorkingSessionStatus.ACTIVE


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _make_active_event(
    session: Session,
    entity: Entity,
    *,
    title: str,
    familiarity: float,
    created_at: datetime,
) -> EventMemory:
    event = EventMemory(
        entity_id=entity.entity_id,
        subject="Project Alpha",
        event_type="decision",
        event_time=created_at,
        title=title,
        raw_content=title,
        embedding=[1.0] + [0.0] * 255,
        source="api",
        content_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        status=EventStatus.ACTIVE,
        visibility=Visibility.PRIVATE,
        owner_id="alice",
        created_at=created_at,
        familiarity_score=familiarity,
    )
    session.add(event)
    return event


def test_retrieval_hit_reinforcement_dedup_next_day_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检索命中强化：增量递减、两层去重、次日可再计、不越过 1.0、不动反馈字段。"""
    settings = Settings(
        _env_file=None,
        environment="test",
        redis_url=None,
        retrieval_hit_familiarity_step=0.02,
        retrieval_hit_dedup_minutes=60,
        new_memory_protect_hours=168,
    )
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        raw_url = postgres.get_connection_url()
        database_url = "postgresql+psycopg://" + raw_url.split("://", 1)[1]
        monkeypatch.setenv("DATABASE_URL", database_url)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        command.upgrade(migration_config(database_url), "head")

        factory = sessionmaker(bind=engine)
        clock = _MutableClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))
        with Session(engine) as session:
            entity = Entity(
                entity_name="Project Alpha",
                entity_type="project",
                owner_id="alice",
                visibility=Visibility.PRIVATE,
            )
            session.add(entity)
            session.flush()
            event_a = _make_active_event(
                session, entity, title="A", familiarity=0.0, created_at=clock.now
            )
            event_b = _make_active_event(
                session, entity, title="B", familiarity=0.0, created_at=clock.now
            )
            event_c = _make_active_event(
                session, entity, title="C", familiarity=0.999, created_at=clock.now
            )
            event_d = _make_active_event(
                session, entity, title="D", familiarity=1.0, created_at=clock.now
            )
            session.commit()
            a_id = event_a.event_id
            b_id = event_b.event_id
            c_id = event_c.event_id
            d_id = event_d.event_id

        service = RetrievalReinforcementService(factory, settings, clock=clock)

        # 首次命中 +0.02（0 + 0.02 × (1 - 0)）。
        assert (
            service.reinforce(
                actor_id="alice", query="Project Alpha 阈值", hits=[("event", a_id)]
            )
            == 1
        )
        # 同查询同日重复：命中两层去重，不重复加分。
        assert (
            service.reinforce(
                actor_id="alice", query="Project Alpha   阈值", hits=[("event", a_id)]
            )
            == 0
        )
        # 换查询措辞（同一查询归一化后不同）当日可再加，且增量递减 0.02 × (1 - 0.02)。
        assert (
            service.reinforce(
                actor_id="alice", query="project alpha threshold", hits=[("event", a_id)]
            )
            == 1
        )

        # 跨午夜：同一指纹 1 小时内即使跨了自然日也不重复计。
        clock.now = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
        assert (
            service.reinforce(actor_id="alice", query="跨午夜查询", hits=[("event", b_id)]) == 1
        )
        clock.advance(minutes=40)
        assert (
            service.reinforce(actor_id="alice", query="跨午夜查询", hits=[("event", b_id)]) == 0
        )
        clock.advance(minutes=30)
        assert (
            service.reinforce(actor_id="alice", query="跨午夜查询", hits=[("event", b_id)]) == 1
        )

        # 同一查询次日可再贡献一次。
        clock.now = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
        assert (
            service.reinforce(
                actor_id="alice", query="Project Alpha 阈值", hits=[("event", a_id)]
            )
            == 1
        )

        # 熟悉度逼近 1.0 但不会超过；已到 1.0 则不再加分也不写审计行。
        clock.now = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)
        assert service.reinforce(actor_id="alice", query="cap query", hits=[("event", c_id)]) == 1
        assert service.reinforce(actor_id="alice", query="cap query", hits=[("event", d_id)]) == 0

        with Session(engine) as check:
            stored_a = check.get(EventMemory, a_id)
            stored_b = check.get(EventMemory, b_id)
            stored_c = check.get(EventMemory, c_id)
            stored_d = check.get(EventMemory, d_id)
            assert stored_a is not None and stored_b is not None
            assert stored_c is not None and stored_d is not None
            assert stored_a.familiarity_score == pytest.approx(0.058808)
            assert stored_b.familiarity_score == pytest.approx(0.0396)
            assert stored_c.familiarity_score == pytest.approx(0.99902)
            assert stored_d.familiarity_score == 1.0
            # 命中强化不推进反馈专属字段。
            assert stored_a.access_count == 0
            assert stored_a.last_access_time is None
            assert (
                check.scalar(
                    select(func.count())
                    .select_from(RetrievalHitGain)
                    .where(RetrievalHitGain.actor_id == "alice")
                )
                == 6
            )


