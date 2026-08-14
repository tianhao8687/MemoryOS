from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Float, case, cast, false, func, or_, select
from sqlalchemy.orm import Session, noload

from memoryos.db.models import (
    ClaimEvidenceRow,
    ClaimRelationRow,
    ClaimRow,
    EntityRow,
    MemoryHealthRow,
    MemoryRow,
    SourceAnchorRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimStaleState,
    ClaimStatus,
    MemoryStatus,
    MemoryTemperature,
    QueryIntent,
    ScopeType,
    SearchRequest,
)
from memoryos.errors import ProviderError
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2.diversity import mmr_select
from memoryos.retrieval_v2.fusion import reciprocal_rank_fusion
from memoryos.retrieval_v2.planner import QueryPlan
from memoryos.retrieval_v2.routing import RetrievalChannel, RetrievalRecipe

LEGACY_SCORE_CONTRACT = "legacy_raw_rrf_v1"
NORMALIZED_SCORE_CONTRACT = "normalized_weighted_rrf_v1"

ChannelStatus = Literal[
    "not_requested",
    "not_applicable",
    "executed",
    "executed_empty",
    "unavailable",
    "provider_fallback",
]
MemoryAllowed = Callable[
    [MemoryRow, SearchRequest, set[tuple[str, str | None]] | None],
    bool,
]


class Reranker(Protocol):
    @property
    def name(self) -> str: ...

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]: ...


@dataclass(frozen=True)
class ChannelExecution:
    channel: RetrievalChannel
    requested: bool
    available: bool
    attempted: bool
    executed: bool
    candidate_count: int
    eligible_candidate_count: int
    status: ChannelStatus
    reason_code: str | None = None
    shared_stage: str | None = None
    duration_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "requested": self.requested,
            "available": self.available,
            "attempted": self.attempted,
            "executed": self.executed,
            "candidate_count": self.candidate_count,
            "eligible_candidate_count": self.eligible_candidate_count,
            "status": self.status,
            "reason_code": self.reason_code,
            "shared_stage": self.shared_stage,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class CandidateStageResult:
    baseline: dict[str, Any]
    by_id: dict[str, dict[str, Any]]
    rankings: dict[str, list[str]]
    channel_execution: tuple[ChannelExecution, ...]
    duration_ms: float


@dataclass(frozen=True)
class FusionStageResult:
    scores: dict[str, float]
    rank_traces: dict[str, dict[str, int]]
    score_contract: str
    duration_ms: float


@dataclass(frozen=True)
class RerankStageResult:
    candidates: list[dict[str, Any]]
    mode: str
    duration_ms: float


@dataclass(frozen=True)
class DiversityStageResult:
    candidates: list[dict[str, Any]]
    duration_ms: float


class CandidateRetrievalStage:
    """Execute channel retrieval while exposing requested versus actual capability state."""

    def __init__(self, database: Database, baseline: RetrievalEngine) -> None:
        self.database = database
        self.baseline = baseline

    def execute(
        self,
        request: SearchRequest,
        plan: QueryPlan,
        recipe: RetrievalRecipe,
        *,
        allowed_scopes: set[tuple[str, str | None]] | None,
        memory_allowed: MemoryAllowed,
    ) -> CandidateStageResult:
        started = time.perf_counter()
        requested = set(recipe.channels)
        baseline_request = request.model_copy(
            update={
                "limit": min(
                    recipe.candidate_pool_max,
                    max(recipe.candidate_pool_min, request.offset + request.limit),
                ),
                "offset": 0,
            }
        )
        baseline_started = time.perf_counter()
        baseline = self.baseline.search(baseline_request, allowed_scopes=allowed_scopes)
        baseline_duration = _duration_ms(baseline_started)
        by_id = {str(item["memory"]["id"]): item for item in baseline["items"]}
        lexical = sorted(
            by_id,
            key=lambda item_id: float(by_id[item_id].get("lexical_score", 0.0)),
            reverse=True,
        )
        semantic = [
            item_id
            for item_id in sorted(
                by_id,
                key=lambda identity: float(by_id[identity].get("semantic_score", 0.0)),
                reverse=True,
            )
            if float(by_id[item_id].get("semantic_score", 0.0)) > 0
        ]

        channel_ids: dict[RetrievalChannel, list[str]] = {
            "fts": lexical,
            "vector": semantic,
            "source_anchor": [],
            "graph": [],
            "temporal": [],
        }
        channel_durations: dict[RetrievalChannel, float | None] = {
            "fts": None,
            "vector": None,
            "source_anchor": None,
            "graph": None,
            "temporal": None,
        }
        with self.database.session() as session:
            if "source_anchor" in requested and plan.exact_terms:
                channel_started = time.perf_counter()
                channel_ids["source_anchor"] = self._source_anchor_candidates(
                    session,
                    plan,
                    limit=recipe.candidate_pool_max,
                )
                channel_durations["source_anchor"] = _duration_ms(channel_started)
            if "graph" in requested and plan.entities:
                channel_started = time.perf_counter()
                channel_ids["graph"] = self._graph_candidates(
                    session,
                    plan,
                    request=request,
                    allowed_scopes=allowed_scopes,
                    limit=recipe.candidate_pool_max,
                )
                channel_durations["graph"] = _duration_ms(channel_started)
            if "temporal" in requested and plan.intent is QueryIntent.HISTORICAL_AS_OF:
                channel_started = time.perf_counter()
                channel_ids["temporal"] = self._temporal_candidates(
                    session,
                    plan,
                    limit=recipe.candidate_pool_max,
                )
                channel_durations["temporal"] = _duration_ms(channel_started)

            extra_ids = {
                identity
                for channel in recipe.channels
                for identity in channel_ids[channel]
                if identity not in by_id
            }
            if extra_ids:
                extras = list(
                    session.scalars(
                        select(MemoryRow)
                        .options(noload(MemoryRow.sources))
                        .where(MemoryRow.id.in_(extra_ids))
                    )
                )
                for row in extras:
                    if not memory_allowed(row, request, allowed_scopes):
                        continue
                    by_id[row.id] = {
                        "memory": self.baseline._serialize(row),
                        "score": 0.0,
                        "lexical_score": 0.0,
                        "semantic_score": 0.0,
                    }

        rankings: dict[str, list[str]] = {
            channel: [identity for identity in channel_ids[channel] if identity in by_id]
            if channel in requested
            else []
            for channel in channel_ids
        }
        executions = tuple(
            self._execution_for(
                channel,
                requested=channel in requested,
                raw_count=len(channel_ids[channel]),
                eligible_count=len(rankings[channel]),
                baseline_mode=str(baseline["mode"]),
                query_present=bool(request.query.strip()),
                applicable=(
                    bool(plan.exact_terms)
                    if channel == "source_anchor"
                    else bool(plan.entities)
                    if channel == "graph"
                    else plan.intent is QueryIntent.HISTORICAL_AS_OF
                    if channel == "temporal"
                    else bool(request.query.strip())
                    if channel == "vector"
                    else True
                ),
                baseline_duration=baseline_duration,
                duration_ms=channel_durations[channel],
            )
            for channel in channel_ids
        )
        return CandidateStageResult(
            baseline=baseline,
            by_id=by_id,
            rankings=rankings,
            channel_execution=executions,
            duration_ms=_duration_ms(started),
        )

    def _execution_for(
        self,
        channel: RetrievalChannel,
        *,
        requested: bool,
        raw_count: int,
        eligible_count: int,
        baseline_mode: str,
        query_present: bool,
        applicable: bool,
        baseline_duration: float,
        duration_ms: float | None,
    ) -> ChannelExecution:
        if not requested:
            return ChannelExecution(
                channel=channel,
                requested=False,
                available=(
                    self.baseline.embedding_provider is not None if channel == "vector" else True
                ),
                attempted=False,
                executed=False,
                candidate_count=0,
                eligible_candidate_count=0,
                status="not_requested",
            )
        if not applicable:
            return ChannelExecution(
                channel=channel,
                requested=True,
                available=(
                    self.baseline.embedding_provider is not None if channel == "vector" else True
                ),
                attempted=False,
                executed=False,
                candidate_count=0,
                eligible_candidate_count=0,
                status="not_applicable",
                reason_code=(
                    "empty_query"
                    if channel == "vector" and not query_present
                    else "no_exact_terms"
                    if channel == "source_anchor"
                    else "no_entities"
                    if channel == "graph"
                    else "non_temporal_intent"
                ),
            )
        if channel == "vector":
            available = self.baseline.embedding_provider is not None
            if not available:
                return ChannelExecution(
                    channel=channel,
                    requested=True,
                    available=False,
                    attempted=False,
                    executed=False,
                    candidate_count=0,
                    eligible_candidate_count=0,
                    status="unavailable",
                    reason_code="embedding_provider_not_configured",
                    shared_stage="lexical_vector",
                    duration_ms=baseline_duration,
                )
            if baseline_mode == "fts5-fallback":
                return ChannelExecution(
                    channel=channel,
                    requested=True,
                    available=True,
                    attempted=True,
                    executed=False,
                    candidate_count=0,
                    eligible_candidate_count=0,
                    status="provider_fallback",
                    reason_code="embedding_provider_failure",
                    shared_stage="lexical_vector",
                    duration_ms=baseline_duration,
                )
            return ChannelExecution(
                channel=channel,
                requested=True,
                available=True,
                attempted=True,
                executed=True,
                candidate_count=raw_count,
                eligible_candidate_count=eligible_count,
                status="executed" if eligible_count else "executed_empty",
                shared_stage="lexical_vector",
                duration_ms=baseline_duration,
            )
        if channel == "fts":
            return ChannelExecution(
                channel=channel,
                requested=True,
                available=True,
                attempted=True,
                executed=True,
                candidate_count=raw_count,
                eligible_candidate_count=eligible_count,
                status="executed" if eligible_count else "executed_empty",
                shared_stage="lexical_vector",
                duration_ms=baseline_duration,
            )
        return ChannelExecution(
            channel=channel,
            requested=True,
            available=True,
            attempted=True,
            executed=True,
            candidate_count=raw_count,
            eligible_candidate_count=eligible_count,
            status="executed" if eligible_count else "executed_empty",
            duration_ms=duration_ms,
        )

    @staticmethod
    def _source_anchor_candidates(
        session: Session,
        plan: QueryPlan,
        *,
        limit: int,
    ) -> list[str]:
        if not plan.exact_terms:
            return []
        metadata_terms = {
            normalized
            for raw_term in plan.exact_terms
            for normalized in (
                raw_term.replace("\\", "/").casefold(),
                raw_term.replace("\\", "/").casefold().rsplit("/", 1)[-1],
            )
            if normalized
        }
        if not metadata_terms:
            return []
        conditions = [
            predicate
            for term in sorted(metadata_terms)
            for predicate in (
                SourceAnchorRow.path.icontains(term, autoescape=True),
                SourceAnchorRow.symbol_fqn.icontains(term, autoescape=True),
            )
        ]
        exact_symbol = [
            func.lower(SourceAnchorRow.symbol_fqn) == term for term in sorted(metadata_terms)
        ]
        exact_path = [func.lower(SourceAnchorRow.path) == term for term in sorted(metadata_terms)]
        symbol_suffix = [
            SourceAnchorRow.symbol_fqn.iendswith(term, autoescape=True)
            for term in sorted(metadata_terms)
        ]
        path_suffix = [
            SourceAnchorRow.path.iendswith(term, autoescape=True) for term in sorted(metadata_terms)
        ]
        rows = session.execute(
            select(ClaimRow.memory_id, SourceAnchorRow)
            .join(ClaimEvidenceRow, ClaimEvidenceRow.claim_id == ClaimRow.id)
            .join(SourceAnchorRow, SourceAnchorRow.id == ClaimEvidenceRow.source_anchor_id)
            .where(or_(*conditions))
            .order_by(
                case((or_(*exact_symbol), 1), else_=0).desc(),
                case((or_(*exact_path), 1), else_=0).desc(),
                case((or_(*symbol_suffix), 1), else_=0).desc(),
                case((or_(*path_suffix), 1), else_=0).desc(),
                SourceAnchorRow.path,
                SourceAnchorRow.symbol_fqn,
                ClaimRow.memory_id,
            )
            .limit(limit)
        ).all()
        ranked: list[tuple[tuple[int, ...], str, str, str]] = []
        for memory_id, anchor in rows:
            match = _anchor_match(anchor, plan.exact_terms)
            if match is None:
                continue
            ranked.append(
                (
                    match,
                    str(anchor.path).casefold(),
                    str(anchor.symbol_fqn or "").casefold(),
                    str(memory_id),
                )
            )
        ranked.sort(
            key=lambda item: (
                tuple(-value for value in item[0]),
                item[1],
                item[2],
                item[3],
            )
        )
        return list(dict.fromkeys(item[3] for item in ranked))

    @staticmethod
    def _graph_candidates(
        session: Session,
        plan: QueryPlan,
        *,
        request: SearchRequest,
        allowed_scopes: set[tuple[str, str | None]] | None,
        limit: int,
    ) -> list[str]:
        if not plan.entities:
            return []
        tokens = sorted(set(plan.entities))
        conditions = [
            predicate
            for token in tokens
            for predicate in (
                EntityRow.normalized_name.icontains(token, autoescape=True),
                ClaimRow.canonical_key.icontains(token, autoescape=True),
            )
        ]
        eligibility = CandidateRetrievalStage._claim_eligibility_conditions(
            request,
            allowed_scopes,
        )
        exact_entity = func.lower(EntityRow.normalized_name).in_(tokens)
        match_priority = case((exact_entity, 2), else_=1)
        per_memory_rank = func.row_number().over(
            partition_by=ClaimRow.memory_id,
            order_by=(match_priority.desc(), ClaimRow.recorded_at.desc(), ClaimRow.id),
        )
        ranked = (
            select(
                ClaimRow.id.label("claim_id"),
                ClaimRow.memory_id.label("memory_id"),
                ClaimRow.recorded_at.label("recorded_at"),
                match_priority.label("match_priority"),
                per_memory_rank.label("memory_rank"),
            )
            .join(EntityRow, EntityRow.id == ClaimRow.subject_entity_id)
            .join(MemoryRow, MemoryRow.id == ClaimRow.memory_id)
            .outerjoin(MemoryHealthRow, MemoryHealthRow.memory_id == MemoryRow.id)
            .where(or_(*conditions), *eligibility)
            .subquery()
        )
        claims = session.execute(
            select(ranked.c.claim_id, ranked.c.memory_id)
            .where(ranked.c.memory_rank == 1)
            .order_by(
                ranked.c.match_priority.desc(),
                ranked.c.recorded_at.desc(),
                ranked.c.claim_id,
            )
            .limit(limit)
        ).all()
        seed_ids = [str(claim_id) for claim_id, _memory_id in claims]
        graph = [str(memory_id) for _claim_id, memory_id in claims]
        if not seed_ids or len(graph) >= limit:
            return list(dict.fromkeys(graph))
        relations = session.execute(
            select(
                ClaimRelationRow.from_claim_id,
                ClaimRelationRow.to_claim_id,
            )
            .where(
                or_(
                    ClaimRelationRow.from_claim_id.in_(seed_ids),
                    ClaimRelationRow.to_claim_id.in_(seed_ids),
                )
            )
            .order_by(ClaimRelationRow.created_at.desc(), ClaimRelationRow.id)
            .limit(limit * 4)
        ).all()
        seed_id_set = set(seed_ids)
        related_ids = list(
            dict.fromkeys(
                str(to_claim_id) if str(from_claim_id) in seed_id_set else str(from_claim_id)
                for from_claim_id, to_claim_id in relations
            )
        )
        if related_ids:
            eligible_related = {
                str(claim_id): str(memory_id)
                for claim_id, memory_id in session.execute(
                    select(ClaimRow.id, ClaimRow.memory_id)
                    .join(EntityRow, EntityRow.id == ClaimRow.subject_entity_id)
                    .join(MemoryRow, MemoryRow.id == ClaimRow.memory_id)
                    .outerjoin(MemoryHealthRow, MemoryHealthRow.memory_id == MemoryRow.id)
                    .where(ClaimRow.id.in_(related_ids), *eligibility)
                ).all()
            }
            graph.extend(
                eligible_related[claim_id]
                for claim_id in related_ids
                if claim_id in eligible_related
            )
        return list(dict.fromkeys(graph))[:limit]

    @staticmethod
    def _claim_eligibility_conditions(
        request: SearchRequest,
        allowed_scopes: set[tuple[str, str | None]] | None,
    ) -> list[Any]:
        valid_moment = request.as_of_valid_time or datetime.now(UTC)
        known_moment = request.as_known_at or datetime.now(UTC)
        conditions: list[Any] = [
            EntityRow.scope_type == MemoryRow.scope_type,
            EntityRow.scope_key == MemoryRow.scope_key,
            or_(MemoryRow.valid_from.is_(None), MemoryRow.valid_from <= valid_moment),
            or_(MemoryRow.valid_to.is_(None), valid_moment < MemoryRow.valid_to),
            or_(ClaimRow.valid_from.is_(None), ClaimRow.valid_from <= valid_moment),
            or_(ClaimRow.valid_to.is_(None), valid_moment < ClaimRow.valid_to),
            MemoryRow.created_at <= known_moment,
            ClaimRow.recorded_at <= known_moment,
            or_(
                MemoryRow.ttl_seconds.is_(None),
                func.julianday(MemoryRow.created_at) + cast(MemoryRow.ttl_seconds, Float) / 86400.0
                > func.julianday(valid_moment),
            ),
        ]
        if request.scope_type is not None:
            conditions.extend(
                (
                    MemoryRow.scope_type == request.scope_type,
                    EntityRow.scope_type == request.scope_type,
                )
            )
        if request.scope_key is not None:
            conditions.extend(
                (
                    MemoryRow.scope_key == request.scope_key,
                    EntityRow.scope_key == request.scope_key,
                )
            )
        if request.status is not None:
            conditions.append(MemoryRow.status == request.status)
        elif not request.include_history:
            conditions.extend(
                (
                    MemoryRow.status == MemoryStatus.ACTIVE,
                    ClaimRow.status.in_([ClaimStatus.ACCEPTED, ClaimStatus.CONTESTED]),
                    ClaimRow.stale_state != ClaimStaleState.STALE,
                    or_(
                        MemoryHealthRow.memory_id.is_(None),
                        MemoryHealthRow.temperature != MemoryTemperature.ARCHIVED,
                    ),
                )
            )
        if allowed_scopes is not None:
            if not allowed_scopes:
                conditions.append(false())
            else:
                scope_conditions = []
                for raw_scope_type, scope_key in sorted(
                    allowed_scopes,
                    key=lambda item: (item[0], item[1] or ""),
                ):
                    scope_type = ScopeType(raw_scope_type)
                    if scope_key is None:
                        scope_conditions.append(MemoryRow.scope_type == scope_type)
                    else:
                        scope_conditions.append(
                            (MemoryRow.scope_type == scope_type)
                            & (MemoryRow.scope_key == scope_key)
                        )
                conditions.append(or_(*scope_conditions))
        return conditions

    @staticmethod
    def _temporal_candidates(session: Session, plan: QueryPlan, *, limit: int) -> list[str]:
        if plan.intent is not QueryIntent.HISTORICAL_AS_OF:
            return []
        return list(
            dict.fromkeys(
                session.scalars(
                    select(ClaimRow.memory_id)
                    .where(
                        ClaimRow.status.in_(
                            [
                                ClaimStatus.ACCEPTED,
                                ClaimStatus.SUPERSEDED,
                                ClaimStatus.HISTORICAL,
                            ]
                        )
                    )
                    .order_by(ClaimRow.recorded_at.desc(), ClaimRow.memory_id)
                    .limit(limit)
                )
            )
        )


class FusionStage:
    def execute(
        self,
        rankings: dict[str, list[str]],
        *,
        weights: dict[str, float],
        k: int,
        normalized: bool,
    ) -> FusionStageResult:
        started = time.perf_counter()
        raw_scores, rank_traces = reciprocal_rank_fusion(rankings, weights=weights, k=k)
        scores = (
            normalize_weighted_rrf(raw_scores, rankings=rankings, weights=weights, k=k)
            if normalized
            else raw_scores
        )
        return FusionStageResult(
            scores=scores,
            rank_traces=rank_traces,
            score_contract=(NORMALIZED_SCORE_CONTRACT if normalized else LEGACY_SCORE_CONTRACT),
            duration_ms=_duration_ms(started),
        )


class RerankStage:
    def __init__(self, reranker: Reranker | None) -> None:
        self.reranker = reranker

    def execute(
        self,
        candidates: list[dict[str, Any]],
        *,
        query: str,
        recipe: RetrievalRecipe,
        routed: bool,
        scoring_profile: Any | None,
    ) -> RerankStageResult:
        started = time.perf_counter()
        mode = "disabled"
        reranker_allowed = recipe.reranker_policy == "cross_encoder_if_available"
        if self.reranker is not None and query.strip() and reranker_allowed:
            try:
                rerank_window = candidates[: recipe.rerank_window]
                reranked = self.reranker.rerank(query, rerank_window)
                expected_ids = {str(item["memory"]["id"]) for item in rerank_window}
                if routed and set(reranked) != expected_ids:
                    raise ProviderError(
                        "routed reranker must score every candidate in its bounded window"
                    )
                if routed and any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not 0.0 <= float(value) <= 1.0
                    for value in reranked.values()
                ):
                    raise ProviderError("routed reranker scores must be finite values in [0, 1]")
                reranked_items = []
                missing_items = []
                for item in rerank_window:
                    identity = str(item["memory"]["id"])
                    if identity not in reranked:
                        missing_items.append(item)
                        continue
                    value = reranked[identity]
                    item["trace"]["reranker_score"] = value
                    if routed:
                        item["fused_score"] = (
                            value
                            * float(item["trace"]["freshness_factor"])
                            * float(item["trace"]["feedback_factor"])
                            * float(item["trace"]["scope_factor"])
                        )
                        item["score"] = round(float(item["fused_score"]), 8)
                    else:
                        item["fused_score"] = (
                            float(item["fused_score"]) * 0.7 + value * 0.3
                            if scoring_profile is None
                            else scoring_profile.score(item["trace"])
                        )
                        if scoring_profile is not None:
                            item["score"] = round(float(item["fused_score"]), 8)
                    reranked_items.append(item)
                if routed:
                    reranked_items.sort(key=lambda item: float(item["fused_score"]), reverse=True)
                    candidates = reranked_items + missing_items + candidates[recipe.rerank_window :]
                else:
                    candidates.sort(key=lambda item: float(item["fused_score"]), reverse=True)
                mode = self.reranker.name
            except ProviderError:
                mode = "provider-fallback"
        elif self.reranker is not None and not reranker_allowed:
            mode = "disabled-by-recipe"
        return RerankStageResult(
            candidates=candidates,
            mode=mode,
            duration_ms=_duration_ms(started),
        )


class DiversityStage:
    def execute(
        self,
        candidates: list[dict[str, Any]],
        *,
        recipe: RetrievalRecipe,
        limit: int,
        lambda_relevance: float,
    ) -> DiversityStageResult:
        started = time.perf_counter()
        selected = (
            mmr_select(candidates, limit=limit, lambda_relevance=lambda_relevance)
            if recipe.diversity_policy == "mmr"
            else candidates[:limit]
        )
        return DiversityStageResult(candidates=selected, duration_ms=_duration_ms(started))


def normalize_weighted_rrf(
    scores: dict[str, float],
    *,
    rankings: dict[str, list[str]],
    weights: dict[str, float],
    k: int,
) -> dict[str, float]:
    """Map weighted RRF into a recipe-independent [0, 1] theoretical score contract."""

    maximum = sum(weights.get(channel, 1.0) / (k + 1) for channel, ids in rankings.items() if ids)
    if maximum <= 0.0:
        return dict(scores)
    return {identity: min(1.0, max(0.0, score / maximum)) for identity, score in scores.items()}


def _anchor_match(anchor: SourceAnchorRow, terms: list[str]) -> tuple[int, ...] | None:
    symbol = str(anchor.symbol_fqn or "").casefold()
    symbol_tail = symbol.rsplit(".", 1)[-1]
    path = str(anchor.path).replace("\\", "/").casefold()
    basename = path.rsplit("/", 1)[-1]
    best: tuple[int, ...] | None = None
    for raw_term in terms:
        term = raw_term.replace("\\", "/").casefold()
        term_tail = term.rsplit("/", 1)[-1]
        match = (
            int(bool(symbol) and term in {symbol, symbol_tail}),
            int(term in {path, basename}),
            int(bool(symbol) and (symbol.endswith(f".{term}") or term in symbol)),
            int(path.endswith(term) or term in path),
            int(term_tail in {symbol_tail, basename}),
        )
        if any(match) and (best is None or match > best):
            best = match
    return best


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = [
    "LEGACY_SCORE_CONTRACT",
    "NORMALIZED_SCORE_CONTRACT",
    "CandidateRetrievalStage",
    "ChannelExecution",
    "DiversityStage",
    "FusionStage",
    "RerankStage",
    "Reranker",
    "normalize_weighted_rrf",
]
