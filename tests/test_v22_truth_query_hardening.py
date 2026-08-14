from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select

from memoryos.claims.versioning import ClaimVersionStore
from memoryos.db import Database
from memoryos.db.models import (
    AuditEventRow,
    ClaimIdentityRow,
    ClaimRow,
    ClaimVersionRow,
    EntityRow,
    MemoryRow,
)
from memoryos.domain.schemas import (
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimStaleState,
    ClaimStatus,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    MemoryStatus,
    MemoryType,
    ScopeType,
    Sensitivity,
)
from memoryos.engine import MemoryService
from memoryos.health import service as health_service_module


def _seed_truth_identities(database: Database, *, scope_key: str, count: int) -> None:
    recorded_at = datetime(2026, 1, 1, tzinfo=UTC)
    entities: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    for index in range(count):
        suffix = f"{count:04d}-{index:04d}"
        entity_id = f"entity-{suffix}"
        memory_id = f"memory-{suffix}"
        identity_id = f"identity-{suffix}"
        claim_id = f"claim-{suffix}"
        entities.append(
            {
                "id": entity_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "entity_type": EntityType.PROJECT,
                "canonical_name": f"Subject {suffix}",
                "normalized_name": f"subject-{suffix}",
                "aliases_json": [],
                "stable_external_key": None,
                "redirect_to_id": None,
                "created_at": recorded_at,
                "updated_at": recorded_at,
            }
        )
        memories.append(
            {
                "id": memory_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "memory_type": MemoryType.PROJECT,
                "category": "decision",
                "subject": None,
                "key": f"truth.{suffix}",
                "title": f"Truth {suffix}",
                "content": f"Subject {suffix} uses value {suffix}.",
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
        )
        identities.append(
            {
                "id": identity_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "subject_entity_id": entity_id,
                "canonical_subject": f"subject-{suffix}",
                "canonical_predicate": "uses",
                "stable_identity": f"{index + count:064x}",
                "created_at": recorded_at,
            }
        )
        claims.append(
            {
                "id": claim_id,
                "memory_id": memory_id,
                "subject_entity_id": entity_id,
                "predicate": "uses",
                "object_kind": ClaimObjectKind.LITERAL,
                "object_entity_id": None,
                "object_value": f"value-{suffix}",
                "polarity": ClaimPolarity.POSITIVE,
                "modality": ClaimModality.DECISION,
                "qualifiers_json": {},
                "canonical_key": f"subject-{suffix}|uses|value-{suffix}|positive",
                "confidence": 0.9,
                "status": ClaimStatus.ACCEPTED,
                "valid_from": None,
                "valid_to": None,
                "recorded_at": recorded_at,
                "stale_state": ClaimStaleState.FRESH,
            }
        )
        versions.append(
            {
                "id": f"version-{suffix}",
                "claim_id": claim_id,
                "identity_id": identity_id,
                "memory_id": memory_id,
                "version_number": 1,
                "object_kind": ClaimObjectKind.LITERAL,
                "object_entity_id": None,
                "object_value": f"value-{suffix}",
                "polarity": ClaimPolarity.POSITIVE,
                "modality": ClaimModality.DECISION,
                "qualifiers_json": {},
                "valid_from": None,
                "valid_to": None,
                "transaction_from": recorded_at,
                "transaction_to": None,
                "status": ClaimStatus.ACCEPTED,
                "stale_state": ClaimStaleState.FRESH,
                "confidence": 0.9,
                "reason": "query hardening fixture",
                "actor": "test",
                "source_event_id": None,
                "created_at": recorded_at,
            }
        )
    with database.session() as session:
        session.execute(EntityRow.__table__.insert(), entities)
        session.execute(MemoryRow.__table__.insert(), memories)
        session.execute(ClaimIdentityRow.__table__.insert(), identities)
        session.execute(ClaimRow.__table__.insert(), claims)
        session.execute(ClaimVersionRow.__table__.insert(), versions)


def _current_truth_with_query_count(
    database: Database,
    service: MemoryService,
    scope_key: str,
) -> tuple[dict[str, Any], int]:
    query_count = 0

    def count_query(*_args: Any, **_kwargs: Any) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        result = service.current_truth(
            CurrentTruthRequest(
                scope_type=ScopeType.REPOSITORY,
                scope_key=scope_key,
                predicate="uses",
            )
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)
    return result, query_count


def test_current_truth_results_scale_without_query_count_scaling(
    database: Database,
    service: MemoryService,
) -> None:
    sizes = (1, 10, 1000)
    for size in sizes:
        _seed_truth_identities(database, scope_key=f"truth-scale-{size}", count=size)

    measured: dict[int, int] = {}
    for size in sizes:
        result, query_count = _current_truth_with_query_count(
            database,
            service,
            f"truth-scale-{size}",
        )
        assert result["state"] == "resolved"
        assert len(result["truths"]) == size
        assert len(result["accepted_claims"]) == size
        assert {item["object_value"] for item in result["accepted_claims"]} == {
            f"value-{size:04d}-{index:04d}" for index in range(size)
        }
        measured[size] = query_count

    assert measured[1000] <= measured[1] + 12
    assert measured[1000] <= measured[10] + 4


def test_visible_versions_uses_half_open_valid_and_transaction_boundaries(
    database: Database,
) -> None:
    scope_key = "truth-half-open"
    _seed_truth_identities(database, scope_key=scope_key, count=1)
    valid_start = datetime(2026, 1, 1, tzinfo=UTC)
    valid_boundary = datetime(2026, 2, 1, tzinfo=UTC)
    transaction_start = datetime(2026, 3, 1, tzinfo=UTC)
    transaction_boundary = datetime(2026, 4, 1, tzinfo=UTC)
    with database.session() as session:
        first = session.get(ClaimVersionRow, "version-0001-0000")
        assert first is not None
        first.object_value = "before"
        first.valid_from = valid_start
        first.valid_to = valid_boundary
        first.transaction_from = transaction_start
        first.transaction_to = transaction_boundary
        session.add(
            ClaimVersionRow(
                id="version-half-open-after",
                claim_id=first.claim_id,
                identity_id=first.identity_id,
                memory_id=first.memory_id,
                version_number=2,
                object_kind=ClaimObjectKind.LITERAL,
                object_value="after",
                polarity=ClaimPolarity.POSITIVE,
                modality=ClaimModality.DECISION,
                qualifiers_json={},
                valid_from=valid_boundary,
                valid_to=None,
                transaction_from=transaction_boundary,
                transaction_to=None,
                status=ClaimStatus.ACCEPTED,
                stale_state=ClaimStaleState.FRESH,
                confidence=0.9,
                reason="half-open boundary",
                actor="test",
                created_at=transaction_boundary,
            )
        )

    store = ClaimVersionStore()
    with database.session() as session:
        before = store.visible_versions(
            session,
            valid_time=valid_boundary - timedelta(microseconds=1),
            known_time=transaction_boundary - timedelta(microseconds=1),
            scope_type=ScopeType.REPOSITORY,
            scope_key=scope_key,
        )
        valid_end_is_exclusive = store.visible_versions(
            session,
            valid_time=valid_boundary,
            known_time=transaction_boundary - timedelta(microseconds=1),
            scope_type=ScopeType.REPOSITORY,
            scope_key=scope_key,
        )
        transaction_end_is_exclusive = store.visible_versions(
            session,
            valid_time=valid_boundary,
            known_time=transaction_boundary,
            scope_type=ScopeType.REPOSITORY,
            scope_key=scope_key,
        )

    assert [row.object_value for row in before] == ["before"]
    assert valid_end_is_exclusive == []
    assert [row.object_value for row in transaction_end_is_exclusive] == ["after"]


def test_archive_restore_audit_times_are_strictly_ordered_when_clock_repeats(
    database: Database,
    service: MemoryService,
    make_memory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            return fixed

    memory = service.propose(
        make_memory(activate_immediately=False),
        actor="archive-order-test",
    )
    monkeypatch.setattr(health_service_module, "datetime", FrozenDateTime)

    service.archive_memory(str(memory["id"]), actor="archive-order-test")
    service.restore_archived_memory(str(memory["id"]), actor="archive-order-test")

    with database.session() as session:
        events = list(
            session.execute(
                select(AuditEventRow.action, AuditEventRow.timestamp)
                .where(
                    AuditEventRow.entity_id == memory["id"],
                    AuditEventRow.action.in_(["health_archive", "health_restore"]),
                )
                .order_by(AuditEventRow.timestamp)
            )
        )

    assert [action for action, _timestamp in events] == ["health_archive", "health_restore"]
    assert events[0][1] < events[1][1]
