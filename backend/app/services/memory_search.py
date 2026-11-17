from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ForbiddenError, NotFoundError
from app.core.logging import redact_text
from app.core.security import CurrentUser
from app.domain.intent import IntentDecision, classify_intent
from app.domain.retrieval import RankedItem, fuse_ranked, fuse_score
from app.domain.scoring import (
    event_base_decay,
    event_memory_weight,
    materialize_familiarity,
    relevance,
    wiki_memory_weight,
)
from app.ml.registry import ModelBundle
from app.models import (
    EventMemory,
    MemoryAccessLog,
    MemoryQuery,
    MemoryType,
    WikiArticle,
    WikiArticleStatus,
    WikiChunk,
    WikiChunkStatus,
    WorkingSession,
)
from app.repositories.permissions import event_candidate_predicate
from app.schemas.memory import (
    EventSearchResult,
    SearchRequest,
    WikiSearchResult,
    WikiSourceRef,
)

MAX_QUERY_LOG_LENGTH = 500


@dataclass(frozen=True)
class SearchOutcome:
    query_id: UUID
    intent: str
    results: list[EventSearchResult | WikiSearchResult]


@dataclass(frozen=True)
class _Candidate:
    item: RankedItem
    result: EventSearchResult | WikiSearchResult
    similarity: float


def _round(value: float) -> float:
    return round(float(value), 6)


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()


class MemorySearchService:
    """统一检索：双路召回、可选精排、融合排序并记录曝光日志。"""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        models: ModelBundle,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session = session
        self.settings = settings
        self.models = models
        self.clock = clock

    def search(self, *, user: CurrentUser, request: SearchRequest) -> SearchOutcome:
        filters = request.filters
        decision = classify_intent(
            request.query,
            memory_type=request.memory_type,
            has_entity_filter=bool(filters.entity_ids),
            has_time_filter=bool(filters.start_time or filters.end_time),
            has_wiki_filter=bool(filters.wiki_categories or filters.wiki_languages),
        )
        self._validate_session(user, request.session_id)
        base_vector = self.models.embedding.embed([request.query])[0]
        query_vector = self.models.query_encoder.encode(base_vector)

        event_candidates: list[_Candidate] = []
        wiki_candidates: list[_Candidate] = []
        if request.memory_type in {"all", "event"}:
            event_candidates = self._recall_events(user, request, query_vector, decision)
        if request.memory_type in {"all", "wiki"}:
            wiki_candidates = self._recall_wiki(request, base_vector, decision)

        ranked = fuse_ranked(
            [candidate.item for candidate in event_candidates + wiki_candidates],
            top_n=request.top_n,
        )
        by_key = {
            (
                candidate.item.memory_type,
                candidate.item.memory_id,
                candidate.item.tie_break,
            ): candidate
            for candidate in event_candidates + wiki_candidates
        }
        selected = [by_key[(item.memory_type, item.memory_id, item.tie_break)] for item in ranked]

        now = self.clock()
        query = MemoryQuery(
            actor_id=user.subject,
            session_id=request.session_id,
            query_text_redacted=redact_text(request.query)[:MAX_QUERY_LOG_LENGTH],
            query_hash=_query_hash(request.query),
            intent=decision.intent,
            filters={
                **filters.model_dump(mode="json"),
                "memory_type": request.memory_type,
                "intent_reason": decision.reason,
            },
            event_candidate_count=len(event_candidates),
            wiki_candidate_count=len(wiki_candidates),
        )
        self.session.add(query)
        self.session.flush()
        for rank, candidate in enumerate(selected):
            self.session.add(
                MemoryAccessLog(
                    query_id=query.query_id,
                    session_id=request.session_id,
                    actor_id=user.subject,
                    memory_type=MemoryType(candidate.item.memory_type),
                    memory_id=candidate.item.memory_id,
                    query_hash=query.query_hash,
                    result_rank=rank,
                    similarity=_round(candidate.similarity),
                    final_score=_round(candidate.item.final_score),
                    feedback_type="exposure",
                    is_counted=False,
                    access_date=now.date(),
                )
            )
        self.session.flush()
        return SearchOutcome(
            query_id=query.query_id,
            intent=decision.intent,
            results=[candidate.result for candidate in selected],
        )

    def _validate_session(self, user: CurrentUser, session_id: UUID | None) -> None:
        if session_id is None:
            return
        session = self.session.get(WorkingSession, session_id)
        if session is None:
            raise NotFoundError("工作记忆会话不存在")
        if session.actor_id != user.subject and not user.is_admin:
            raise ForbiddenError("不能访问其他用户的工作记忆会话")

    def _recall_events(
        self,
        user: CurrentUser,
        request: SearchRequest,
        query_vector: list[float],
        decision: IntentDecision,
    ) -> list[_Candidate]:
        filters = request.filters
        distance = EventMemory.embedding.cosine_distance(query_vector).label("distance")
        statement: Select = select(EventMemory, distance).where(
            event_candidate_predicate(user),
            EventMemory.embedding.is_not(None),
            distance <= 1 - self.settings.search_min_similarity,
        )
        if filters.subject:
            statement = statement.where(EventMemory.subject == filters.subject)
        if filters.entity_ids:
            statement = statement.where(EventMemory.entity_id.in_(filters.entity_ids))
        if filters.event_types:
            statement = statement.where(EventMemory.event_type.in_(filters.event_types))
        if filters.start_time:
            statement = statement.where(EventMemory.event_time >= filters.start_time)
        if filters.end_time:
            statement = statement.where(EventMemory.event_time <= filters.end_time)
        rows = self.session.execute(
            statement.order_by(distance).limit(self.settings.search_candidate_k)
        ).all()
        if not rows:
            return []

        documents = [self._event_document(event) for event, _ in rows]
        rerank_scores = self._rerank(request.query, documents)
        candidates: list[_Candidate] = []
        for index, (event, distance_value) in enumerate(rows):
            cosine_similarity = max(-1.0, min(1.0, 1.0 - float(distance_value)))
            rerank_score = rerank_scores[index] if rerank_scores is not None else None
            relevance_score = relevance(cosine_similarity, rerank_score)
            decay = event_base_decay(
                event.initial_decay_score,
                event.created_at,
                clock=self.clock,
                decay_lambda=self.settings.decay_lambda,
            )
            familiarity_now = materialize_familiarity(
                event.familiarity_score,
                anchor=event.familiarity_updated_at,
                created_at=event.created_at,
                clock=self.clock,
                gamma=self.settings.familiarity_gamma,
                max_score=self.settings.familiarity_max,
                protect_hours=self.settings.new_memory_protect_hours,
            ).score
            ranking_familiarity = familiarity_now if request.enable_familiarity else 0.0
            weight = event_memory_weight(
                decay,
                ranking_familiarity,
                cap=self.settings.memory_weight_cap,
                familiarity_beta=self.settings.familiarity_beta,
            )
            final_score = fuse_score(decision.event_weight, relevance_score, weight)
            breakdown = {
                "cosine_similarity": _round(cosine_similarity),
                "relevance": _round(relevance_score),
                "decay_score": _round(decay),
                "familiarity_score": _round(familiarity_now),
                "memory_weight": _round(weight),
                "intent_weight": _round(decision.event_weight),
            }
            if rerank_score is not None:
                breakdown["rerank_score"] = _round(rerank_score)
            result = EventSearchResult(
                memory_id=event.event_id,
                title=event.title,
                content=event.raw_content or "",
                event_time=event.event_time,
                final_score=_round(final_score),
                score_breakdown=breakdown if request.include_score_breakdown else None,
                model_version=event.event_model_version,
            )
            candidates.append(
                _Candidate(
                    item=RankedItem(
                        memory_type="event",
                        memory_id=event.event_id,
                        final_score=final_score,
                        tie_break=event.title,
                    ),
                    result=result,
                    similarity=cosine_similarity,
                )
            )
        return candidates

    def _recall_wiki(
        self,
        request: SearchRequest,
        query_vector: list[float],
        decision: IntentDecision,
    ) -> list[_Candidate]:
        filters = request.filters
        distance = WikiChunk.embedding.cosine_distance(query_vector).label("distance")
        statement: Select = (
            select(WikiChunk, WikiArticle, distance)
            .join(WikiArticle, WikiArticle.article_id == WikiChunk.article_id)
            .where(
                WikiChunk.status == WikiChunkStatus.ACTIVE,
                WikiArticle.status == WikiArticleStatus.ACTIVE,
                WikiChunk.embedding.is_not(None),
                distance <= 1 - self.settings.search_min_similarity,
            )
        )
        if filters.wiki_categories:
            for category in filters.wiki_categories:
                statement = statement.where(WikiArticle.categories.contains([category]))
        if filters.wiki_languages:
            statement = statement.where(WikiArticle.language.in_(filters.wiki_languages))
        rows = self.session.execute(
            statement.order_by(distance).limit(self.settings.search_candidate_k)
        ).all()
        if not rows:
            return []

        documents = [chunk.raw_text for chunk, _, _ in rows]
        rerank_scores = self._rerank(request.query, documents)
        candidates: list[_Candidate] = []
        for index, (chunk, article, distance_value) in enumerate(rows):
            cosine_similarity = max(-1.0, min(1.0, 1.0 - float(distance_value)))
            rerank_score = rerank_scores[index] if rerank_scores is not None else None
            relevance_score = relevance(cosine_similarity, rerank_score)
            familiarity_now = materialize_familiarity(
                chunk.familiarity_score,
                anchor=chunk.familiarity_updated_at,
                created_at=chunk.created_at,
                clock=self.clock,
                gamma=self.settings.familiarity_gamma,
                max_score=self.settings.familiarity_max,
                protect_hours=self.settings.new_memory_protect_hours,
            ).score
            ranking_familiarity = familiarity_now if request.enable_familiarity else 0.0
            weight = wiki_memory_weight(
                chunk.importance,
                ranking_familiarity,
                cap=self.settings.memory_weight_cap,
                wiki_familiarity_alpha=self.settings.wiki_familiarity_alpha,
            )
            final_score = fuse_score(decision.wiki_weight, relevance_score, weight)
            breakdown = {
                "cosine_similarity": _round(cosine_similarity),
                "importance": _round(chunk.importance),
                "familiarity_score": _round(familiarity_now),
                "memory_weight": _round(weight),
                "intent_weight": _round(decision.wiki_weight),
            }
            if rerank_score is not None:
                breakdown["rerank_score"] = _round(rerank_score)
            result = WikiSearchResult(
                memory_id=chunk.chunk_id,
                article_id=article.article_id,
                title=article.title,
                content=chunk.raw_text,
                chunk_index=chunk.chunk_index,
                heading_path=list(chunk.heading_path or []),
                source=WikiSourceRef(
                    name=article.source,
                    url=article.source_url,
                    page_id=article.source_page_id,
                    revision_id=article.revision_id,
                ),
                final_score=_round(final_score),
                score_breakdown=breakdown if request.include_score_breakdown else None,
                model_version=chunk.model_version,
            )
            candidates.append(
                _Candidate(
                    item=RankedItem(
                        memory_type="wiki",
                        memory_id=chunk.chunk_id,
                        article_id=article.article_id,
                        final_score=final_score,
                        tie_break=f"{article.article_id}:{chunk.chunk_index}",
                    ),
                    result=result,
                    similarity=cosine_similarity,
                )
            )
        return candidates

    def _rerank(self, query: str, documents: list[str]) -> list[float] | None:
        if not documents or self.models.reranker is None:
            return None
        scores = self.models.reranker.rerank(query, documents)
        if len(scores) != len(documents):
            raise AppError("RERANK_INVALID", "Rerank 分数数量与文档数量不一致", 502)
        return [max(0.0, min(1.0, float(score))) for score in scores]

    @staticmethod
    def _event_document(event: EventMemory) -> str:
        return f"{event.title}\n{event.raw_content or ''}".strip()
