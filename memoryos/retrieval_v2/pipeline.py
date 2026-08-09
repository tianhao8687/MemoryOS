from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Protocol

from sqlalchemy import func, or_, select

from memoryos.db.models import (
    ClaimEvidenceRow,
    ClaimRelationRow,
    ClaimRow,
    EntityRow,
    MemoryFeedbackRow,
    MemoryRow,
    RetrievalRunRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimStaleState,
    ClaimStatus,
    FeedbackValue,
    QueryIntent,
    SearchRequest,
)
from memoryos.errors import ProviderError
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2.diversity import mmr_select
from memoryos.retrieval_v2.fusion import reciprocal_rank_fusion
from memoryos.retrieval_v2.planner import QueryPlan, plan_query


class Reranker(Protocol):
    @property
    def name(self) -> str: ...

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]: ...


RRF_WEIGHTS = {"fts": 1.0, "vector": 1.0, "graph": 0.82, "temporal": 0.9}
RRF_K = 60


def _scope_factor(scope_type: str) -> float:
    return {"task": 1.0, "branch": 0.96, "repository": 0.9, "workspace": 0.78, "user": 0.7}.get(
        scope_type, 0.6
    )


class RetrievalPipeline:
    def __init__(
        self,
        database: Database,
        baseline: RetrievalEngine,
        reranker: Reranker | None = None,
    ) -> None:
        self.database = database
        self.baseline = baseline
        self.reranker = reranker
        self.config_hash = hashlib.sha256(
            json.dumps(
                {"rrf": RRF_WEIGHTS, "k": RRF_K, "mmr_lambda": 0.78},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

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
        baseline_request = request.model_copy(
            update={"limit": min(500, max(200, request.limit * 10)), "offset": 0}
        )
        baseline = self.baseline.search(baseline_request, allowed_scopes=allowed_scopes)
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
        with self.database.session() as session:
            graph_ids, temporal_ids = self._claim_candidates(session, plan)
            extra_ids = (set(graph_ids) | set(temporal_ids)) - set(by_id)
            if extra_ids:
                extras = list(session.scalars(select(MemoryRow).where(MemoryRow.id.in_(extra_ids))))
                for row in extras:
                    if not self._memory_allowed(row, request, allowed_scopes):
                        continue
                    by_id[row.id] = {
                        "memory": self.baseline._serialize(row),
                        "score": 0.0,
                        "lexical_score": 0.0,
                        "semantic_score": 0.0,
                    }
            rankings = {
                "fts": [identity for identity in lexical if identity in by_id],
                "vector": [identity for identity in semantic if identity in by_id],
                "graph": [identity for identity in graph_ids if identity in by_id],
                "temporal": [identity for identity in temporal_ids if identity in by_id],
            }
            fused, rank_traces = reciprocal_rank_fusion(rankings, weights=RRF_WEIGHTS, k=RRF_K)
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
            feedback = self._feedback_factors(session, list(by_id))
            candidates = []
            for memory_id, item in by_id.items():
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
                final_score = (
                    base_fused
                    * freshness_factor
                    * feedback.get(memory_id, 1.0)
                    * _scope_factor(str(memory["scope_type"]))
                )
                ranks = rank_traces.get(memory_id, {})
                reasons = [
                    channel
                    for channel in ("fts", "vector", "graph", "temporal")
                    if channel in ranks
                ]
                if freshness == "suspect":
                    reasons.append("suspect freshness downweighted")
                candidates.append(
                    {
                        **item,
                        "score": round(final_score, 8),
                        "fused_score": final_score,
                        "truth_state": truth_state,
                        "claim_ids": [claim.id for claim in claims],
                        "trace": {
                            "fts_rank": ranks.get("fts"),
                            "vector_rank": ranks.get("vector"),
                            "graph_rank": ranks.get("graph"),
                            "temporal_rank": ranks.get("temporal"),
                            "fused_score": round(base_fused, 8),
                            "scope_match": memory["scope_type"],
                            "freshness": freshness,
                            "evidence_count": int(evidence_counts.get(memory_id, 0)),
                            "reranker_score": None,
                            "final_reason": reasons or ["baseline fallback"],
                        },
                    }
                )
            candidates.sort(key=lambda item: float(item["fused_score"]), reverse=True)
            reranker_mode = "disabled"
            if self.reranker is not None and request.query.strip():
                try:
                    reranked = self.reranker.rerank(request.query, candidates[:40])
                    for item in candidates[:40]:
                        identity = str(item["memory"]["id"])
                        if identity in reranked:
                            value = reranked[identity]
                            item["trace"]["reranker_score"] = value
                            item["fused_score"] = float(item["fused_score"]) * 0.7 + value * 0.3
                    candidates.sort(key=lambda item: float(item["fused_score"]), reverse=True)
                    reranker_mode = self.reranker.name
                except ProviderError:
                    reranker_mode = "provider-fallback"
            total = len(candidates)
            diverse = mmr_select(candidates, limit=min(total, request.offset + request.limit))
            selected = diverse[request.offset : request.offset + request.limit]
            run = RetrievalRunRow(
                query=request.query,
                task=task,
                scope_json={"allowed": sorted(allowed_scopes or [])},
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
                "pipeline_mode": f"rrf-{baseline['mode']}",
                "reranker": reranker_mode,
                "query_plan": plan.model_dump(),
                "retrieval_run_id": run.id,
                "config_hash": self.config_hash,
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
        return request.include_history or row.status.value == "active"

    @staticmethod
    def _claim_candidates(session: Any, plan: QueryPlan) -> tuple[list[str], list[str]]:
        if not plan.entities and plan.intent is not QueryIntent.HISTORICAL_AS_OF:
            return [], []
        statement = select(ClaimRow, EntityRow).join(
            EntityRow, EntityRow.id == ClaimRow.subject_entity_id
        )
        claims = session.execute(statement.limit(5000)).all()
        seed_claims = []
        temporal = []
        for claim, entity in claims:
            object_text = str(claim.object_value or "").lower()
            entity_match = any(
                token in entity.normalized_name or token in object_text for token in plan.entities
            )
            if entity_match:
                seed_claims.append(claim)
            if plan.intent is QueryIntent.HISTORICAL_AS_OF and claim.status in {
                ClaimStatus.ACCEPTED,
                ClaimStatus.SUPERSEDED,
                ClaimStatus.HISTORICAL,
            }:
                temporal.append(claim.memory_id)
        graph = [claim.memory_id for claim in seed_claims]
        if seed_claims:
            seed_ids = [claim.id for claim in seed_claims]
            relations = list(
                session.scalars(
                    select(ClaimRelationRow).where(
                        or_(
                            ClaimRelationRow.from_claim_id.in_(seed_ids),
                            ClaimRelationRow.to_claim_id.in_(seed_ids),
                        )
                    )
                )
            )
            related_ids = {
                relation.to_claim_id
                if relation.from_claim_id in seed_ids
                else relation.from_claim_id
                for relation in relations
            }
            if related_ids:
                graph.extend(
                    session.scalars(select(ClaimRow.memory_id).where(ClaimRow.id.in_(related_ids)))
                )
        return list(dict.fromkeys(graph)), list(dict.fromkeys(temporal))

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
        factors: dict[str, float] = {}
        if not memory_ids:
            return factors
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
        for memory_id, helpful, count in rows:
            if helpful is FeedbackValue.YES:
                scores[memory_id] += min(0.2, 0.03 * count)
            elif helpful is FeedbackValue.NO:
                scores[memory_id] -= min(0.35, 0.06 * count)
        for memory_id, delta in scores.items():
            factors[memory_id] = max(0.5, 1.0 + delta)
        return factors
