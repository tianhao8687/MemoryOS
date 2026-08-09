from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from memoryos.claims.canonicalize import extract_claim_candidates
from memoryos.claims.predicates import compare_claim_values
from memoryos.db.models import ClaimRow, EntityRow
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimPolarity,
    ConflictStrategy,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.entities import EntityResolver


@pytest.mark.v2
def test_a15_claim_normalization_splits_multiple_evidence_bound_claims() -> None:
    text = (
        "We decided to use PostgreSQL in production because concurrent workers caused SQLite "
        "write contention. Do not add Redis in V1."
    )
    claims = extract_claim_candidates(
        text,
        title="Production architecture",
        category="decision",
        key="architecture.production",
    )
    predicates = {claim.predicate for claim in claims}
    assert {"uses", "failed_because", "forbidden"}.issubset(predicates)
    for claim in claims:
        span = claim.evidence_span
        assert text[span.start : span.end] == span.quote


@pytest.mark.v2
@pytest.mark.parametrize("rewrite", ["PostgreSQL", " postgresql ", "POSTGRES", "PostgreSQL DB"])
def test_harmless_claim_rewrites_remain_equivalent(rewrite: str) -> None:
    relation = compare_claim_values(
        left_subject="production database",
        left_predicate="use",
        left_object="PostgreSQL",
        left_polarity=ClaimPolarity.POSITIVE,
        right_subject="production database",
        right_predicate="uses",
        right_object=rewrite,
        right_polarity=ClaimPolarity.POSITIVE,
    )

    assert relation == "equivalent"


@pytest.mark.v2
def test_a16_entity_resolution_links_aliases_without_cross_scope_or_type_merge(
    database: Database,
) -> None:
    resolver = EntityResolver()
    with database.session() as session:
        postgres = resolver.resolve(
            session,
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            entity_type=EntityType.DATABASE,
            name="Postgres",
        )
        postgresql = resolver.resolve(
            session,
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            entity_type=EntityType.DATABASE,
            name="PostgreSQL",
        )
        other_repo = resolver.resolve(
            session,
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-b",
            entity_type=EntityType.DATABASE,
            name="Postgres",
        )
        concept = resolver.resolve(
            session,
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            entity_type=EntityType.CONCEPT,
            name="Postgres",
        )
        assert postgres.id == postgresql.id
        assert other_repo.id != postgres.id
        assert concept.id != postgres.id
        assert len(list(session.scalars(select(EntityRow)))) == 3


@pytest.mark.v2
def test_a17_a18_semantic_conflict_across_keys_yields_contested_truth(
    service: MemoryService, make_memory: Any
) -> None:
    current = service.propose(
        make_memory(
            title="Use FastAPI",
            content="The backend framework uses FastAPI.",
            key="architecture.runtime.web",
        ),
        actor="test",
    )
    candidate = service.propose(
        make_memory(
            title="Choose Django",
            content="The backend framework uses Django instead.",
            key="implementation.server.framework",
            created_by=CreatedBy.AGENT,
            activate_immediately=False,
            source_ref="agent:semantic-conflict",
        ),
        actor="test",
    )
    conflicts = service.conflicts()
    assert conflicts[0]["candidate"]["id"] == candidate["id"]
    assert current["id"] in {item["id"] for item in conflicts[0]["current"]}
    service.confirm(
        candidate["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        rationale="Needs human architecture review",
        actor="test",
    )
    truth = service.current_truth(
        CurrentTruthRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            subject="project.backend_framework",
            predicate="uses",
        )
    )
    assert truth["state"] == "contested"
    assert {item["memory_id"] for item in truth["conflicting_claims"]} == {
        current["id"],
        candidate["id"],
    }


def _temporal_memory(
    *,
    title: str,
    content: str,
    key: str,
    valid_from: datetime,
    valid_to: datetime | None,
) -> MemoryCreate:
    return MemoryCreate(
        scope_type=ScopeType.REPOSITORY,
        scope_key="repo-temporal",
        memory_type=MemoryType.PROJECT,
        category="decision",
        key=key,
        title=title,
        content=content,
        valid_from=valid_from,
        valid_to=valid_to,
        created_by=CreatedBy.MANUAL,
        activate_immediately=True,
        source=SourceCreate(
            source_type=SourceType.MANUAL,
            source_ref=f"temporal:{key}",
            captured_at=valid_from,
            excerpt=content,
        ),
    )


@pytest.mark.v2
def test_a19_bitemporal_valid_time_and_known_at_are_distinct(
    database: Database, service: MemoryService
) -> None:
    august_1 = datetime(2026, 8, 1, tzinfo=UTC)
    august_2 = datetime(2026, 8, 2, tzinfo=UTC)
    august_5 = datetime(2026, 8, 5, tzinfo=UTC)
    august_6 = datetime(2026, 8, 6, tzinfo=UTC)
    august_7 = datetime(2026, 8, 7, tzinfo=UTC)
    august_8 = datetime(2026, 8, 8, tzinfo=UTC)
    old = service.propose(
        _temporal_memory(
            title="Use SQLite",
            content="The production database uses SQLite.",
            key="database.before",
            valid_from=august_1,
            valid_to=august_5,
        ),
        actor="test",
    )
    new = service.propose(
        _temporal_memory(
            title="Use PostgreSQL",
            content="The production database uses PostgreSQL.",
            key="database.after",
            valid_from=august_5,
            valid_to=None,
        ),
        actor="test",
    )
    with database.session() as session:
        old_claim = session.scalar(select(ClaimRow).where(ClaimRow.memory_id == old["id"]))
        new_claim = session.scalar(select(ClaimRow).where(ClaimRow.memory_id == new["id"]))
        assert old_claim is not None and new_claim is not None
        old_claim.recorded_at = august_2
        new_claim.recorded_at = august_7

    selector = {
        "scope_type": ScopeType.REPOSITORY,
        "scope_key": "repo-temporal",
        "subject": "project.production_database",
        "predicate": "uses",
        "as_of_valid_time": august_6,
    }
    not_yet_known = service.current_truth(CurrentTruthRequest(**selector, as_known_at=august_5))
    known_later = service.current_truth(CurrentTruthRequest(**selector, as_known_at=august_8))
    historical = service.current_truth(
        CurrentTruthRequest(**{**selector, "as_of_valid_time": august_2}, as_known_at=august_8)
    )
    assert not_yet_known["state"] == "unknown"
    assert known_later["accepted_claims"][0]["object_value"] == "postgresql"
    assert historical["accepted_claims"][0]["object_value"] == "sqlite"
