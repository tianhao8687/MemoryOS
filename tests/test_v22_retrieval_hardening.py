from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memoryos.db.models import ClaimRelationRow, ClaimRow, EntityRow, MemoryRow
from memoryos.domain.schemas import (
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimRelationType,
    ClaimStaleState,
    ClaimStatus,
    CreatedBy,
    EntityType,
    MemoryStatus,
    MemoryType,
    RelationMethod,
    ScopeType,
    SearchRequest,
    Sensitivity,
)
from memoryos.engine import MemoryService
from memoryos.retrieval_v2.planner import plan_query
from memoryos.retrieval_v2.stages import CandidateRetrievalStage


def _memory_payload(
    identity: str,
    scope_key: str,
    recorded_at: datetime,
) -> dict[str, Any]:
    return {
        "id": identity,
        "scope_type": ScopeType.REPOSITORY,
        "scope_key": scope_key,
        "memory_type": MemoryType.PROJECT,
        "category": "decision",
        "subject": None,
        "key": f"hardening.{identity}",
        "title": f"Opaque decision {identity}",
        "content": "An intentionally opaque relation-channel record.",
        "status": MemoryStatus.ACTIVE,
        "confidence": 0.9,
        "importance": 0.8,
        "valid_from": None,
        "valid_to": None,
        "ttl_seconds": None,
        "supersedes_id": None,
        "created_at": recorded_at,
        "updated_at": recorded_at,
        "created_by": CreatedBy.MANUAL,
        "sensitivity": Sensitivity.NORMAL,
        "metadata_json": {},
    }


def _claim_payload(
    identity: str,
    memory_id: str,
    entity_id: str,
    recorded_at: datetime,
    *,
    canonical_subject: str = "redis",
) -> dict[str, Any]:
    return {
        "id": identity,
        "memory_id": memory_id,
        "subject_entity_id": entity_id,
        "predicate": "uses",
        "object_kind": ClaimObjectKind.LITERAL,
        "object_entity_id": None,
        "object_value": "opaque-value",
        "polarity": ClaimPolarity.POSITIVE,
        "modality": ClaimModality.DECISION,
        "qualifiers_json": {},
        "canonical_key": f"{canonical_subject}|uses|opaque-value|positive|{identity}",
        "confidence": 0.9,
        "status": ClaimStatus.ACCEPTED,
        "valid_from": None,
        "valid_to": None,
        "recorded_at": recorded_at,
        "stale_state": ClaimStaleState.FRESH,
    }


def test_claim_relation_channel_filters_scope_before_large_candidate_limit(
    database: Any,
    service: MemoryService,
) -> None:
    now = datetime.now(UTC)
    target_memory_id = "target-memory"
    target_entity_id = "target-entity"
    decoy_entity_id = "decoy-entity"
    with database.session() as session:
        session.execute(
            EntityRow.__table__.insert(),
            [
                {
                    "id": target_entity_id,
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": "repo-target",
                    "entity_type": EntityType.DEPENDENCY,
                    "canonical_name": "Redis",
                    "normalized_name": "redis",
                    "aliases_json": [],
                    "stable_external_key": None,
                    "redirect_to_id": None,
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": decoy_entity_id,
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": "repo-decoy",
                    "entity_type": EntityType.DEPENDENCY,
                    "canonical_name": "Redis",
                    "normalized_name": "redis",
                    "aliases_json": [],
                    "stable_external_key": None,
                    "redirect_to_id": None,
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        decoy_time = now - timedelta(days=1)
        session.execute(
            MemoryRow.__table__.insert(),
            [
                _memory_payload(target_memory_id, "repo-target", now - timedelta(days=2)),
                *[
                    _memory_payload(f"decoy-memory-{index:04d}", "repo-decoy", decoy_time)
                    for index in range(6001)
                ],
            ],
        )
        session.execute(
            ClaimRow.__table__.insert(),
            [
                _claim_payload(
                    "target-claim",
                    target_memory_id,
                    target_entity_id,
                    now - timedelta(days=2),
                ),
                *[
                    _claim_payload(
                        f"decoy-claim-{index:04d}",
                        f"decoy-memory-{index:04d}",
                        decoy_entity_id,
                        decoy_time,
                    )
                    for index in range(6001)
                ],
            ],
        )

    result = service.search(
        SearchRequest(
            query="why redis",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-target",
            limit=5,
        )
    )

    assert [item["memory"]["id"] for item in result["items"]] == [target_memory_id]
    graph_execution = next(
        item
        for item in result["query_plan"]["routing"]["channel_execution"]
        if item["channel"] == "graph"
    )
    assert graph_execution["candidate_count"] == 1
    assert graph_execution["eligible_candidate_count"] == 1


def test_claim_relation_channel_is_deterministic_and_stops_after_one_hop(
    database: Any,
) -> None:
    now = datetime.now(UTC)
    scope_key = "repo-one-hop"
    with database.session() as session:
        session.execute(
            EntityRow.__table__.insert(),
            [
                {
                    "id": f"entity-{name}",
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": scope_key,
                    "entity_type": EntityType.DEPENDENCY,
                    "canonical_name": name.title(),
                    "normalized_name": name,
                    "aliases_json": [],
                    "stable_external_key": None,
                    "redirect_to_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for name in ("redis", "queue", "worker")
            ],
        )
        memory_ids = ("seed-memory-a", "seed-memory-b", "hop-one-memory", "hop-two-memory")
        session.execute(
            MemoryRow.__table__.insert(),
            [_memory_payload(memory_id, scope_key, now) for memory_id in memory_ids],
        )
        session.execute(
            ClaimRow.__table__.insert(),
            [
                _claim_payload("seed-claim-a", "seed-memory-a", "entity-redis", now),
                _claim_payload("seed-claim-b", "seed-memory-b", "entity-redis", now),
                _claim_payload(
                    "hop-one-claim",
                    "hop-one-memory",
                    "entity-queue",
                    now,
                    canonical_subject="queue",
                ),
                _claim_payload(
                    "hop-two-claim",
                    "hop-two-memory",
                    "entity-worker",
                    now,
                    canonical_subject="worker",
                ),
            ],
        )
        session.execute(
            ClaimRelationRow.__table__.insert(),
            [
                {
                    "id": "relation-one-hop",
                    "from_claim_id": "seed-claim-a",
                    "to_claim_id": "hop-one-claim",
                    "relation_type": ClaimRelationType.DEPENDS_ON,
                    "confidence": 1.0,
                    "method": RelationMethod.MANUAL,
                    "explanation": "first hop",
                    "created_at": now,
                },
                {
                    "id": "relation-two-hop",
                    "from_claim_id": "hop-one-claim",
                    "to_claim_id": "hop-two-claim",
                    "relation_type": ClaimRelationType.DEPENDS_ON,
                    "confidence": 1.0,
                    "method": RelationMethod.MANUAL,
                    "explanation": "must not be traversed from the seed",
                    "created_at": now,
                },
            ],
        )

    plan = plan_query("why redis")
    request = SearchRequest(
        query="why redis",
        scope_type=ScopeType.REPOSITORY,
        scope_key=scope_key,
    )
    with database.session() as session:
        first = CandidateRetrievalStage._graph_candidates(
            session,
            plan,
            request=request,
            allowed_scopes=None,
            limit=10,
        )
        second = CandidateRetrievalStage._graph_candidates(
            session,
            plan,
            request=request,
            allowed_scopes=None,
            limit=10,
        )

    assert first == ["seed-memory-a", "seed-memory-b", "hop-one-memory"]
    assert second == first
    assert "hop-two-memory" not in first
