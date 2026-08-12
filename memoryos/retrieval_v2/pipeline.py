from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from memoryos.db.models import (
    ClaimEvidenceRow,
    ClaimRow,
    MemoryFeedbackRow,
    MemoryHealthRow,
    MemoryRow,
    RetrievalRunRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimStaleState,
    ClaimStatus,
    FeedbackValue,
    MemoryTemperature,
    QueryIntent,
    SearchRequest,
)
from memoryos.health import MemoryHealthService
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2.planner import plan_query
from memoryos.retrieval_v2.routing import (
    APPROVED_RETRIEVAL_RECIPES,
    SAFE_RECIPE_ID,
    RetrievalRecipe,
    RetrievalRoutingShadowProfile,
)
from memoryos.retrieval_v2.rrf_shadow import (
    FROZEN_MMR_LAMBDA,
    FROZEN_RRF_K,
    FROZEN_RRF_WEIGHTS,
    RRFChannelShadowProfile,
)
from memoryos.retrieval_v2.scoring import ShadowRetrievalProfile
from memoryos.retrieval_v2.stages import (
    CandidateRetrievalStage,
    DiversityStage,
    FusionStage,
    Reranker,
    RerankStage,
)
from memoryos.temporal.intervals import as_of, is_known_at

RRF_WEIGHTS = FROZEN_RRF_WEIGHTS
RRF_K = FROZEN_RRF_K


def _scope_factor(scope_type: str) -> float:
    return {"task": 1.0, "branch": 0.96, "repository": 0.9, "workspace": 0.78, "user": 0.7}.get(
        scope_type, 0.6
    )


def retrieval_config_hash(
    scoring_profile: ShadowRetrievalProfile | None = None,
    *,
    rrf_channel_profile: RRFChannelShadowProfile | None = None,
    routing_profile: RetrievalRoutingShadowProfile | None = None,
) -> str:
    if (
        sum(
            profile is not None
            for profile in (scoring_profile, rrf_channel_profile, routing_profile)
        )
        > 1
    ):
        raise ValueError("retrieval can use only one shadow profile at a time")
    config_payload: dict[str, Any] = {
        "rrf": RRF_WEIGHTS,
        "k": RRF_K,
        "mmr_lambda": FROZEN_MMR_LAMBDA,
    }
    if scoring_profile is not None:
        config_payload["shadow_profile"] = scoring_profile.model_dump(mode="json")
    if rrf_channel_profile is not None:
        config_payload["rrf_channel_shadow_profile"] = rrf_channel_profile.model_dump(mode="json")
    if routing_profile is not None:
        config_payload["retrieval_routing_shadow_profile"] = routing_profile.model_dump(mode="json")
    return hashlib.sha256(json.dumps(config_payload, sort_keys=True).encode("utf-8")).hexdigest()


class RetrievalPipeline:
    def __init__(
        self,
        database: Database,
        baseline: RetrievalEngine,
        reranker: Reranker | None = None,
        *,
        scoring_profile: ShadowRetrievalProfile | None = None,
        rrf_channel_profile: RRFChannelShadowProfile | None = None,
        routing_profile: RetrievalRoutingShadowProfile | None = None,
    ) -> None:
        if (
            sum(
                profile is not None
                for profile in (scoring_profile, rrf_channel_profile, routing_profile)
            )
            > 1
        ):
            raise ValueError("retrieval can use only one shadow profile at a time")
        self.database = database
        self.baseline = baseline
        self.scoring_profile = scoring_profile
        self.rrf_channel_profile = rrf_channel_profile
        self.routing_profile = routing_profile
        self.candidate_stage = CandidateRetrievalStage(database, baseline)
        self.fusion_stage = FusionStage()
        self.rerank_stage = RerankStage(reranker)
        self.diversity_stage = DiversityStage()
        self.config_hash = retrieval_config_hash(
            scoring_profile,
            rrf_channel_profile=rrf_channel_profile,
            routing_profile=routing_profile,
        )

    def search(
        self,
        request: SearchRequest,
        *,
        allowed_scopes: set[tuple[str, str | None]] | None = None,
        task: str | None = None,
        repository: str | None = None,
        branch: str | None = None,
        workspace: str | None = None,
        task_scope: str | None = None,
        record_retrieval: bool = True,
    ) -> dict[str, Any]:
        plan = plan_query(
            request.query,
            repository=repository,
            branch=branch,
            workspace=workspace,
            task_scope=task_scope,
            as_of_valid_time=request.as_of_valid_time,
            as_known_at=request.as_known_at,
        )
        recommended_recipe = APPROVED_RETRIEVAL_RECIPES[plan.routing.recommended_recipe_id]
        execution_recipe: RetrievalRecipe = (
            recommended_recipe
            if self.routing_profile is not None
            else APPROVED_RETRIEVAL_RECIPES[SAFE_RECIPE_ID]
        )
        candidate_result = self.candidate_stage.execute(
            request,
            plan,
            execution_recipe,
            allowed_scopes=allowed_scopes,
            memory_allowed=self._memory_allowed,
        )
        baseline = candidate_result.baseline
        by_id = candidate_result.by_id
        rankings = candidate_result.rankings
        rrf_weights = (
            RRF_WEIGHTS
            if self.rrf_channel_profile is None
            else self.rrf_channel_profile.channel_weights
        )
        fusion_weights = dict(rrf_weights)
        if "source_anchor" in execution_recipe.channels:
            fusion_weights["source_anchor"] = rrf_weights["fts"]
        rrf_k = RRF_K if self.rrf_channel_profile is None else self.rrf_channel_profile.rrf_k
        fusion_result = self.fusion_stage.execute(
            rankings,
            weights=fusion_weights,
            k=rrf_k,
            normalized=self.routing_profile is not None,
        )
        fused = fusion_result.scores
        rank_traces = fusion_result.rank_traces
        with self.database.session() as session:
            governance_started = time.perf_counter()
            claim_rows = list(
                session.scalars(select(ClaimRow).where(ClaimRow.memory_id.in_(list(by_id))))
            )
            claims_by_memory: dict[str, list[ClaimRow]] = defaultdict(list)
            for claim in claim_rows:
                claims_by_memory[claim.memory_id].append(claim)
            evidence_counts: dict[str, int] = {
                memory_id: int(count)
                for memory_id, count in session.execute(
                    select(ClaimRow.memory_id, func.count(ClaimEvidenceRow.id))
                    .join(ClaimEvidenceRow, ClaimEvidenceRow.claim_id == ClaimRow.id)
                    .where(ClaimRow.memory_id.in_(list(by_id)))
                    .group_by(ClaimRow.memory_id)
                ).all()
            }
            feedback, feedback_signals = self._feedback_signals(session, list(by_id))
            archived_ids = set(
                session.scalars(
                    select(MemoryHealthRow.memory_id).where(
                        MemoryHealthRow.memory_id.in_(list(by_id)),
                        MemoryHealthRow.temperature == MemoryTemperature.ARCHIVED,
                    )
                )
            )
            candidates = []
            for memory_id, item in by_id.items():
                if memory_id in archived_ids and not request.include_history:
                    continue
                claims = claims_by_memory.get(memory_id, [])
                freshness = self._freshness(claims)
                truth_state = self._truth_state(claims)
                if freshness == "stale" and not (
                    request.include_history or plan.intent is QueryIntent.HISTORICAL_AS_OF
                ):
                    continue
                base_fused = fused.get(memory_id, float(item.get("score", 0.0)) / 100.0)
                freshness_factor = {"fresh": 1.0, "unknown": 0.86, "suspect": 0.35, "stale": 0.0}[
                    freshness
                ]
                memory = item["memory"]
                feedback_factor = feedback.get(memory_id, 1.0)
                scope_factor = _scope_factor(str(memory["scope_type"]))
                ranks = rank_traces.get(memory_id, {})
                reasons = [
                    channel
                    for channel in ("fts", "vector", "source_anchor", "graph", "temporal")
                    if channel in ranks
                ]
                if freshness == "suspect":
                    reasons.append("suspect freshness downweighted")
                trace = {
                    "fts_rank": ranks.get("fts"),
                    "vector_rank": ranks.get("vector"),
                    "graph_rank": ranks.get("graph"),
                    "temporal_rank": ranks.get("temporal"),
                    "fused_score": round(base_fused, 8),
                    "scope_match": memory["scope_type"],
                    "scope_factor": scope_factor,
                    "freshness": freshness,
                    "freshness_factor": freshness_factor,
                    "truth_state": truth_state,
                    "evidence_count": int(evidence_counts.get(memory_id, 0)),
                    "feedback_factor": feedback_factor,
                    "helpful_feedback_count": feedback_signals.get(memory_id, {}).get("helpful", 0),
                    "unhelpful_feedback_count": feedback_signals.get(memory_id, {}).get(
                        "unhelpful", 0
                    ),
                    "memory_confidence": float(memory.get("confidence", 0.5)),
                    "memory_importance": float(memory.get("importance", 0.5)),
                    "reranker_score": None,
                    "final_reason": reasons or ["baseline fallback"],
                }
                if "source_anchor" in execution_recipe.channels:
                    trace["source_anchor_rank"] = ranks.get("source_anchor")
                final_score = (
                    base_fused * freshness_factor * feedback_factor * scope_factor
                    if self.scoring_profile is None
                    else self.scoring_profile.score(trace)
                )
                candidates.append(
                    {
                        **item,
                        "score": round(final_score, 8),
                        "fused_score": final_score,
                        "truth_state": truth_state,
                        "claim_ids": [claim.id for claim in claims],
                        "trace": trace,
                    }
                )
            candidates.sort(key=lambda item: float(item["fused_score"]), reverse=True)
            governance_duration = round((time.perf_counter() - governance_started) * 1000.0, 3)
            rerank_result = self.rerank_stage.execute(
                candidates,
                query=request.query,
                recipe=execution_recipe,
                routed=self.routing_profile is not None,
                scoring_profile=self.scoring_profile,
            )
            candidates = rerank_result.candidates
            total = max(int(baseline["total"]), len(candidates))
            selection_limit = min(total, request.offset + request.limit)
            diversity_result = self.diversity_stage.execute(
                candidates,
                recipe=execution_recipe,
                limit=selection_limit,
                lambda_relevance=(
                    self.scoring_profile.mmr_lambda
                    if self.scoring_profile is not None
                    else FROZEN_MMR_LAMBDA
                    if self.rrf_channel_profile is None
                    else self.rrf_channel_profile.mmr_lambda
                ),
            )
            selected = diversity_result.candidates[request.offset : request.offset + request.limit]
            channel_execution = [item.as_dict() for item in candidate_result.channel_execution]
            executed_channels = [
                item.channel for item in candidate_result.channel_execution if item.executed
            ]
            contributing_channels = [
                item.channel
                for item in candidate_result.channel_execution
                if item.executed and item.eligible_candidate_count > 0
            ]
            degraded_channels = [
                item.channel
                for item in candidate_result.channel_execution
                if item.status in {"unavailable", "provider_fallback"}
            ]
            plan_payload = plan.model_dump()
            plan_payload["routing"].update(
                {
                    "execution_mode": (
                        "candidate_shadow"
                        if self.routing_profile is not None
                        else "frozen_production_baseline"
                    ),
                    "executed_recipe_id": execution_recipe.recipe_id,
                    "executed_recipe_sha256": execution_recipe.digest(),
                    "active_channels": list(execution_recipe.channels),
                    "requested_channels": list(execution_recipe.channels),
                    "executed_channels": executed_channels,
                    "contributing_channels": contributing_channels,
                    "degraded_channels": degraded_channels,
                    "channel_execution": channel_execution,
                    "fusion": execution_recipe.fusion,
                    "fusion_weights": {
                        channel: fusion_weights[channel] for channel in execution_recipe.channels
                    },
                    "rrf_k": rrf_k,
                    "score_contract": fusion_result.score_contract,
                    "source_anchor_weight_policy": (
                        self.routing_profile.source_anchor_weight_policy
                        if self.routing_profile is not None
                        else None
                    ),
                    "reranker_policy": execution_recipe.reranker_policy,
                    "reranker_mode": rerank_result.mode,
                    "diversity_policy": execution_recipe.diversity_policy,
                    "candidate_pool_min": execution_recipe.candidate_pool_min,
                    "candidate_pool_max": execution_recipe.candidate_pool_max,
                    "rerank_window": execution_recipe.rerank_window,
                    "fallback_recipe_id": SAFE_RECIPE_ID,
                    "stage_timings_ms": {
                        "candidate_retrieval": candidate_result.duration_ms,
                        "fusion": fusion_result.duration_ms,
                        "governance_scoring": governance_duration,
                        "rerank": rerank_result.duration_ms,
                        "diversity": diversity_result.duration_ms,
                    },
                }
            )
            if record_retrieval:
                MemoryHealthService.record_retrieval(
                    session,
                    [str(item["memory"]["id"]) for item in selected],
                )
            run = RetrievalRunRow(
                query=request.query,
                task=task,
                scope_json={
                    "allowed": sorted(allowed_scopes or []),
                    "retrieval_plan": plan_payload,
                },
                selected_memory_ids=[str(item["memory"]["id"]) for item in selected],
                candidate_features=[
                    {"memory_id": item["memory"]["id"], **item["trace"]} for item in candidates
                ],
                context_manifest=[],
                config_hash=self.config_hash,
            )
            session.add(run)
            session.flush()
            return {
                "items": selected,
                "total": total,
                "mode": baseline["mode"],
                "pipeline_mode": (
                    f"shadow-profile-{baseline['mode']}"
                    if self.scoring_profile is not None
                    else f"rrf-channel-shadow-{baseline['mode']}"
                    if self.rrf_channel_profile is not None
                    else f"routing-shadow-{execution_recipe.route.value}-{baseline['mode']}"
                    if self.routing_profile is not None
                    else f"rrf-{baseline['mode']}"
                ),
                "reranker": rerank_result.mode,
                "query_plan": plan_payload,
                "retrieval_run_id": run.id,
                "config_hash": self.config_hash,
                "scoring_profile_sha256": (
                    self.scoring_profile.digest()
                    if self.scoring_profile is not None
                    else self.rrf_channel_profile.digest()
                    if self.rrf_channel_profile is not None
                    else None
                ),
                "routing_profile_sha256": (
                    self.routing_profile.digest() if self.routing_profile is not None else None
                ),
            }

    @staticmethod
    def _memory_allowed(
        row: MemoryRow,
        request: SearchRequest,
        allowed_scopes: set[tuple[str, str | None]] | None,
    ) -> bool:
        if allowed_scopes is not None and (
            (row.scope_type.value, row.scope_key) not in allowed_scopes
            and (row.scope_type.value, None) not in allowed_scopes
        ):
            return False
        if request.scope_type is not None and row.scope_type != request.scope_type:
            return False
        if request.scope_key is not None and row.scope_key != request.scope_key:
            return False
        if request.memory_type is not None and row.memory_type != request.memory_type:
            return False
        if request.status is not None and row.status != request.status:
            return False
        if not request.include_history and row.status.value != "active":
            return False
        valid_moment = request.as_of_valid_time or datetime.now(UTC)
        if not as_of(row.valid_from, row.valid_to, valid_moment):
            return False
        valid_moment_utc = (
            valid_moment.replace(tzinfo=UTC)
            if valid_moment.tzinfo is None
            else valid_moment.astimezone(UTC)
        )
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if (
            row.ttl_seconds is not None
            and created_at + timedelta(seconds=row.ttl_seconds) <= valid_moment_utc
        ):
            return False
        return request.as_known_at is None or is_known_at(row.created_at, request.as_known_at)

    @staticmethod
    def _freshness(claims: list[ClaimRow]) -> str:
        states = {claim.stale_state for claim in claims}
        if ClaimStaleState.STALE in states:
            return "stale"
        if ClaimStaleState.SUSPECT in states:
            return "suspect"
        if ClaimStaleState.FRESH in states:
            return "fresh"
        return "unknown"

    @staticmethod
    def _truth_state(claims: list[ClaimRow]) -> str:
        statuses = {claim.status for claim in claims}
        if ClaimStatus.CONTESTED in statuses:
            return "contested"
        if ClaimStatus.STALE in statuses or ClaimStaleState.STALE in {
            claim.stale_state for claim in claims
        }:
            return "stale"
        if ClaimStatus.ACCEPTED in statuses:
            return "resolved"
        return "unknown"

    @staticmethod
    def _feedback_factors(session: Any, memory_ids: list[str]) -> dict[str, float]:
        factors, _ = RetrievalPipeline._feedback_signals(session, memory_ids)
        return factors

    @staticmethod
    def _feedback_signals(
        session: Any,
        memory_ids: list[str],
    ) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
        factors: dict[str, float] = {}
        if not memory_ids:
            return factors, {}
        rows = session.execute(
            select(
                MemoryFeedbackRow.memory_id,
                MemoryFeedbackRow.helpful,
                func.count(MemoryFeedbackRow.id),
            )
            .where(MemoryFeedbackRow.memory_id.in_(memory_ids))
            .group_by(MemoryFeedbackRow.memory_id, MemoryFeedbackRow.helpful)
        ).all()
        scores: dict[str, float] = defaultdict(float)
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"helpful": 0, "unhelpful": 0})
        for memory_id, helpful, count in rows:
            if helpful is FeedbackValue.YES:
                scores[memory_id] += min(0.2, 0.03 * count)
                counts[memory_id]["helpful"] += int(count)
            elif helpful is FeedbackValue.NO:
                scores[memory_id] -= min(0.35, 0.06 * count)
                counts[memory_id]["unhelpful"] += int(count)
        for memory_id, delta in scores.items():
            factors[memory_id] = max(0.5, 1.0 + delta)
        return factors, dict(counts)
