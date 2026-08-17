from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from memoryos.config import MemoryOSSettings, settings_for
from memoryos.context.atoms import AtomBuilder, CompressionPolicy, ContextAtom, exact_deduplicate
from memoryos.context.compiler import TaskAwareContextCompiler
from memoryos.context.token_meter import (
    FunctionTokenCounter,
    TokenCounter,
    UnicodeHeuristicTokenCounter,
    canonical_json,
)
from memoryos.db import Database
from memoryos.db.models import ClaimEvidenceRow, ClaimRow, ContextSnapshotRow, RetrievalRunRow
from memoryos.domain.schemas import (
    BudgetProfile,
    ClaimCandidate,
    ClaimObjectKind,
    ClaimStaleState,
    ConflictStrategy,
    ContextRequest,
    CreatedBy,
    DetailLevel,
    EntityType,
    EvidenceSpan,
)
from memoryos.engine import MemoryService
from memoryos.errors import ContextChangedError, InsufficientBudgetError, TokenizerUnavailableError


@pytest.fixture
def msc_settings(tmp_path: Path) -> MemoryOSSettings:
    return settings_for(tmp_path / "msc-data", context_compiler_mode="msc")


@pytest.fixture
def msc_runtime(msc_settings: MemoryOSSettings) -> Iterator[tuple[Database, MemoryService]]:
    database = Database(msc_settings)
    database.initialize()
    service = MemoryService(database, msc_settings)
    yield database, service
    database.close()


def test_context_request_keeps_legacy_character_budget_and_separates_token_choice() -> None:
    request = ContextRequest(task="fix", repository="repo", budget=4321)

    assert request.budget == 4321
    assert request.budget_tokens is None
    assert request.budget_profile is BudgetProfile.AUTO
    with pytest.raises(ValueError, match="mutually exclusive"):
        ContextRequest(
            task="fix",
            repository="repo",
            budget_tokens=100,
            budget_profile=BudgetProfile.SMALL,
        )
    with pytest.raises(ValueError, match="memory_explain"):
        ContextRequest(
            task="fix",
            repository="repo",
            detail_level=DetailLevel.EVIDENCE,
        )


def test_context_budget_profiles_must_remain_monotonic(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="monotonic"):
        settings_for(
            tmp_path / "invalid-profiles",
            context_budget_tiny_tokens=1000,
            context_budget_small_tokens=500,
        )


def test_estimated_counter_is_stable_and_never_claims_exactness() -> None:
    counter = UnicodeHeuristicTokenCounter()
    value = {"中文": "constraint", "number": 30}

    assert counter.count_json(value) == counter.count_json(value)
    assert counter.kind.value == "estimated"
    assert counter.tokenizer_id == "unicode-heuristic-v1"


def test_canonical_counter_normalizes_equivalent_aware_datetimes() -> None:
    from datetime import UTC, datetime, timedelta, timezone

    counter = UnicodeHeuristicTokenCounter()
    utc_value = datetime(2026, 8, 15, 12, tzinfo=UTC)
    shifted_value = datetime(2026, 8, 15, 20, tzinfo=timezone(timedelta(hours=8)))

    assert canonical_json({"at": utc_value}) == canonical_json({"at": shifted_value})
    assert counter.count_json({"at": utc_value}) == counter.count_json({"at": shifted_value})


@pytest.mark.v23
def test_msc_response_is_thin_and_budget_covers_the_complete_payload(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    database, service = msc_runtime
    memory = service.propose(
        make_memory(
            title="Timeout constraint",
            content="请求超时不得超过 30 秒, 批处理任务除外。",
            category="constraint",
            key="runtime.timeout.constraint",
        ),
        actor="test",
    )

    result = service.context(
        ContextRequest(
            task="检查请求超时约束 30 秒",
            repository="repo-a",
            budget_tokens=5000,
        )
    )

    assert set(result) == {
        "schema_version",
        "mode",
        "context_id",
        "requires_base_context_id",
        "retrieval_run_id",
        "truth_state",
        "text",
        "usage",
    }
    assert result["schema_version"] == "2.3"
    assert result["mode"] == "full"
    assert all(key not in result for key in ("sections", "manifest", "query_plan", "debug"))
    assert "不得超过 30 秒" in result["text"]
    assert "批处理任务除外" in result["text"]
    assert result["usage"]["delivered_payload_tokens"] <= 5000
    assert result["usage"]["context_compilation_llm_input_tokens"] == 0
    assert result["usage"]["context_compilation_llm_output_tokens"] == 0
    assert re.search(rf"\[{memory['id']} @ [0-9a-f]{{64}}\]", result["text"])

    debug = service.debug_context(retrieval_run_id=result["retrieval_run_id"])
    assert debug["context_diagnostics"]["query_plan"]
    assert debug["context_diagnostics"]["legacy_manifest"]
    assert debug["context_diagnostics"]["msc_manifest"]
    with database.session() as session:
        assert session.get(ContextSnapshotRow, result["context_id"]) is not None


@pytest.mark.v23
def test_hard_budget_rejects_estimated_or_missing_exact_tokenizer(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    _, service = msc_runtime
    service.propose(make_memory(), actor="test")

    with pytest.raises(TokenizerUnavailableError):
        service.context(
            ContextRequest(
                task="FastAPI",
                repository="repo-a",
                budget_tokens=1000,
                hard_token_budget=True,
            )
        )
    with pytest.raises(TokenizerUnavailableError):
        service.context(
            ContextRequest(
                task="FastAPI",
                repository="repo-a",
                budget_tokens=1000,
                tokenizer_id="provider-tokenizer-not-installed",
                hard_token_budget=True,
            )
        )


@pytest.mark.v23
def test_exact_hard_budget_returns_minimum_safe_tokens_without_splitting_constraint(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    settings = settings_for(tmp_path / "exact-data", context_compiler_mode="msc")
    database = Database(settings)
    database.initialize()
    normalize_latency = lambda text: len(  # noqa: E731 - compact deterministic fixture counter
        re.sub(r'"(?:selection|render)_latency_ms":[0-9.]+', '"latency":0', text)
    )
    counter = FunctionTokenCounter(
        tokenizer_id="fixture-exact-v1",
        counter_version="1",
        count=normalize_latency,
    )
    service = MemoryService(database, settings, token_counter=counter)
    try:
        service.propose(
            make_memory(
                title="Pinned timeout",
                content="Timeout must not exceed 30 seconds, except offline migration jobs.",
                category="constraint",
                key="timeout.pinned",
            ),
            actor="test",
        )
        first = service.context(
            ContextRequest(
                task="timeout constraint",
                repository="repo-a",
                budget_tokens=50_000,
                tokenizer_id="fixture-exact-v1",
                hard_token_budget=True,
            )
        )
        debug = service.retrieval_run(first["retrieval_run_id"])
        minimum = debug["context_policy_manifest"]["budget"]["minimum_safe_tokens"]

        with pytest.raises(InsufficientBudgetError) as raised:
            service.context(
                ContextRequest(
                    task="timeout constraint",
                    repository="repo-a",
                    budget_tokens=minimum - 1,
                    tokenizer_id="fixture-exact-v1",
                    hard_token_budget=True,
                )
            )
        assert raised.value.details["minimum_safe_tokens"] >= minimum - 1
    finally:
        database.close()


@pytest.mark.v23
def test_complete_payload_respects_a_table_of_exact_hard_budgets(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    settings = settings_for(tmp_path / "budget-table", context_compiler_mode="msc")
    database = Database(settings)
    database.initialize()
    counter = FunctionTokenCounter(
        tokenizer_id="fixture-exact-v1",
        counter_version="1",
        count=lambda text: len(
            re.sub(r'"(?:selection|render)_latency_ms":[0-9.]+', '"latency":0', text)
        ),
    )
    service = MemoryService(database, settings, token_counter=counter)
    try:
        service.propose(
            make_memory(
                title="Timeout constraint",
                content="Production timeout must not exceed 30 seconds.",
                category="constraint",
                key="timeout.constraint",
            ),
            actor="test",
        )
        for index in range(6):
            service.propose(
                make_memory(
                    title=f"Optional timeout decision {index}",
                    content=f"Optional timeout implementation note {index}.",
                    key=f"timeout.optional.{index}",
                ),
                actor="test",
            )
        initial = service.context(
            ContextRequest(
                task="lookup the timeout constraint and implementation decision",
                repository="repo-a",
                budget_tokens=50_000,
                tokenizer_id="fixture-exact-v1",
                hard_token_budget=True,
            )
        )
        manifest = service.retrieval_run(initial["retrieval_run_id"])
        minimum = manifest["context_policy_manifest"]["budget"]["minimum_safe_tokens"]

        with pytest.raises(InsufficientBudgetError):
            service.context(
                ContextRequest(
                    task="lookup the timeout constraint and implementation decision",
                    repository="repo-a",
                    budget_tokens=minimum - 1,
                    tokenizer_id="fixture-exact-v1",
                    hard_token_budget=True,
                )
            )

        for budget in (minimum, minimum + 1, minimum + 100, 50_000):
            response = service.context(
                ContextRequest(
                    task="lookup the timeout constraint and implementation decision",
                    repository="repo-a",
                    budget_tokens=budget,
                    tokenizer_id="fixture-exact-v1",
                    hard_token_budget=True,
                )
            )
            assert response["usage"]["delivered_payload_tokens"] <= budget
            assert "must not exceed 30 seconds" in response["text"]
    finally:
        database.close()


@pytest.mark.v23
def test_contested_bundle_one_token_short_returns_error_instead_of_one_side(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    settings = settings_for(tmp_path / "contested-data", context_compiler_mode="msc")
    database = Database(settings)
    database.initialize()
    counter = FunctionTokenCounter(
        tokenizer_id="fixture-exact-v1",
        counter_version="1",
        count=lambda text: len(
            re.sub(r'"(?:selection|render)_latency_ms":[0-9.]+', '"latency":0', text)
        ),
    )
    service = MemoryService(database, settings, token_counter=counter)
    try:
        left = service.propose(
            make_memory(
                title="Use FastAPI",
                content="The backend framework uses FastAPI.",
                key="decision.framework.a",
            ),
            actor="test",
        )
        right_candidate = service.propose(
            make_memory(
                title="Use Django",
                content="The backend framework uses Django.",
                key="decision.framework.b",
                created_by=CreatedBy.AGENT,
                activate_immediately=False,
                source_ref="agent:contested-v23",
            ),
            actor="test",
        )
        right = service.confirm(
            right_candidate["id"],
            strategy=ConflictStrategy.KEEP_BOTH,
            rationale="Unresolved migration decision",
            actor="test",
        )
        first = service.context(
            ContextRequest(
                task="current backend framework FastAPI Django",
                repository="repo-a",
                budget_tokens=50_000,
                tokenizer_id="fixture-exact-v1",
                hard_token_budget=True,
            )
        )
        assert left["id"] in first["text"]
        assert right["id"] in first["text"]
        debug = service.retrieval_run(first["retrieval_run_id"])
        minimum = debug["context_policy_manifest"]["budget"]["minimum_safe_tokens"]

        with pytest.raises(InsufficientBudgetError) as raised:
            service.context(
                ContextRequest(
                    task="current backend framework FastAPI Django",
                    repository="repo-a",
                    budget_tokens=minimum - 1,
                    tokenizer_id="fixture-exact-v1",
                    hard_token_budget=True,
                )
            )
        assert raised.value.details["minimum_safe_tokens"] >= minimum - 1
    finally:
        database.close()


@pytest.mark.v23
def test_delta_delivers_only_new_atom_and_cross_repository_cursor_safely_rebases(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    _, service = msc_runtime
    for name in ("Alpha", "Beta", "Gamma"):
        service.propose(
            make_memory(
                title=f"{name} decision",
                content=f"The project uses {name}.",
                key=f"decision.{name.lower()}",
            ),
            actor="test",
        )
    first = service.context(
        ContextRequest(
            task="Alpha Beta Gamma Delta decisions",
            repository="repo-a",
            budget_tokens=5000,
        )
    )
    delta_memory = service.propose(
        make_memory(
            title="Delta decision",
            content="The project uses Delta.",
            key="decision.delta",
        ),
        actor="test",
    )
    second = service.context(
        ContextRequest(
            task="Alpha Beta Gamma Delta decisions",
            repository="repo-a",
            budget_tokens=5000,
            previous_context_id=first["context_id"],
            response_mode="delta",
        )
    )

    assert second["mode"] == "delta"
    assert second["requires_base_context_id"] == first["context_id"]
    assert second["delta"]["added"] == [delta_memory["id"]]
    assert "Delta" in second["text"]
    assert "The project uses Alpha" not in second["text"]

    other = service.propose(
        make_memory(
            title="Other repository canary",
            content="PRIVATE-REPO-B-CANARY",
            scope_key="repo-b",
            key="repo-b.canary",
        ),
        actor="test",
    )
    rebased = service.context(
        ContextRequest(
            task="Other repository canary",
            repository="repo-b",
            budget_tokens=5000,
            previous_context_id=second["context_id"],
            response_mode="delta",
        )
    )

    assert rebased["mode"] == "full"
    assert rebased["fallback_reason"] == "scope_mismatch"
    assert other["id"] in rebased["text"]
    assert delta_memory["id"] not in rebased["text"]


@pytest.mark.v23
def test_freshness_change_invalidates_delta_and_old_explain_hash(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    database, service = msc_runtime
    memory = service.propose(
        make_memory(
            title="Fresh decision",
            content="Use the fresh implementation path.",
            key="fresh.path",
        ),
        actor="test",
    )
    for index in range(6):
        service.propose(
            make_memory(
                title=f"Supporting path {index}",
                content=f"Supporting implementation path {index} remains active.",
                key=f"fresh.support.{index}",
            ),
            actor="test",
        )
    first = service.context(
        ContextRequest(
            task="fresh supporting implementation path",
            repository="repo-a",
            budget_tokens=5000,
        )
    )
    atom_hash = re.search(r" @ ([0-9a-f]{64})\]", first["text"])
    assert atom_hash is not None
    old_hash = atom_hash.group(1)

    with database.session() as session:
        claim = session.scalar(select(ClaimRow).where(ClaimRow.memory_id == memory["id"]))
        assert claim is not None
        claim.stale_state = ClaimStaleState.SUSPECT

    second = service.context(
        ContextRequest(
            task="fresh supporting implementation path",
            repository="repo-a",
            budget_tokens=5000,
            previous_context_id=first["context_id"],
            response_mode="delta",
        )
    )
    assert second["mode"] == "delta"
    assert memory["id"] in second["delta"]["changed"]
    assert "suspect" in second["text"]
    with pytest.raises(ContextChangedError):
        service.explain(memory["id"], expected_atom_sha256=old_hash)


def test_exact_dedup_merges_identical_fact_sources_but_never_opposite_polarity() -> None:
    counter = UnicodeHeuristicTokenCounter()
    builder = AtomBuilder(counter)

    def candidate(
        memory_id: str,
        polarity: str,
        source: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        claim_id = f"claim-{memory_id}"
        item = {
            "memory": {
                "id": memory_id,
                "category": "decision",
                "status": "active",
                "memory_type": "project",
                "title": "Refund decision",
                "content": "Refunds use RefundService.",
                "valid_from": None,
                "valid_to": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            "score": 1.0,
            "truth_state": "resolved",
            "trace": {"freshness": "fresh", "evidence_count": 1},
        }
        metadata = {
            "source_refs": [source],
            "evidence_pointers": [{"source": source, "hash": source * 2}],
            "claims": [
                {
                    "id": claim_id,
                    "subject_entity_id": "refund-service",
                    "subject": "RefundService",
                    "predicate": "is_refund_entry",
                    "object_kind": "literal",
                    "object_entity_id": None,
                    "object_name": None,
                    "object_value": True,
                    "polarity": polarity,
                    "modality": "decision",
                    "qualifiers": {},
                    "status": "accepted",
                    "valid_from": None,
                    "valid_to": None,
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
        return item, metadata

    positive_atoms = [
        builder.build(*candidate(memory_id, "positive", source))[0]
        for memory_id, source in (("m1", "s1"), ("m2", "s2"), ("m3", "s3"))
    ]
    deduplicated, duplicate_of = exact_deduplicate(positive_atoms, counter)

    assert len(deduplicated) == 1
    assert deduplicated[0].evidence_count == 3
    assert set(deduplicated[0].source_refs) == {"s1", "s2", "s3"}
    assert set(duplicate_of) == {"m2", "m3"}

    negative = builder.build(*candidate("m4", "negative", "s4"))[0]
    mixed, _ = exact_deduplicate([positive_atoms[0], negative], counter)
    assert len(mixed) == 2
    assert {atom.polarity for atom in mixed} == {"positive", "negative"}

    unique_m2 = positive_atoms[1].model_copy(
        update={
            "canonical_key": "f" * 64,
            "atom_sha256": "e" * 64,
            "fact_text": "RefundService has a second independent property.",
        }
    )
    selected, _ = exact_deduplicate([*positive_atoms, unique_m2], counter)
    manifest = TaskAwareContextCompiler._msc_manifest(
        {
            "manifest": [
                {
                    "memory_id": memory_id,
                    "truth_state": "resolved",
                    "freshness": "fresh",
                }
                for memory_id in ("m1", "m2", "m3")
            ]
        },
        [*positive_atoms, unique_m2],
        selected,
    )
    by_memory = {item["memory_id"]: item for item in manifest}
    assert by_memory["m2"]["included"] is True
    assert by_memory["m2"]["exclusion_reason"] is None
    assert by_memory["m2"]["duplicate_of_memory_ids"] == ["m1"]
    assert [atom["exclusion_reason"] for atom in by_memory["m2"]["atoms"]] == [
        "duplicate",
        None,
    ]
    assert by_memory["m3"]["included"] is False
    assert by_memory["m3"]["exclusion_reason"] == "duplicate"
    assert by_memory["m3"]["atoms"][0]["duplicate_of_memory_id"] == "m1"


def test_atom_builder_uses_only_retrieval_approved_claims_and_stable_pointer_order() -> None:
    counter = UnicodeHeuristicTokenCounter()
    builder = AtomBuilder(counter)
    candidate = {
        "memory": {
            "id": "approved-memory",
            "category": "decision",
            "status": "active",
            "memory_type": "project",
            "title": "Approved claim",
            "content": "Only the approved claim may be compiled.",
            "valid_from": None,
            "valid_to": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        "score": 1.0,
        "truth_state": "resolved",
        "claim_ids": ["claim-approved"],
        "trace": {"freshness": "fresh", "evidence_count": 2},
    }
    base_claim = {
        "subject_entity_id": "project",
        "subject": "Project",
        "predicate": "uses",
        "object_kind": "literal",
        "object_entity_id": None,
        "object_name": None,
        "polarity": "positive",
        "modality": "decision",
        "qualifiers": {},
        "status": "accepted",
        "valid_from": None,
        "valid_to": None,
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }
    pointers = [
        {
            "source_id": "generic-source",
            "source_ref": "generic-source",
            "content_hash": "0" * 64,
        },
        {
            "claim_id": "claim-future",
            "source_id": "source-b",
            "source_ref": "source-b",
            "content_hash": "b" * 64,
        },
        {
            "claim_id": "claim-approved",
            "source_id": "source-a",
            "source_ref": "source-a",
            "content_hash": "a" * 64,
        },
    ]
    metadata = {
        "source_refs": ["source-a", "source-b"],
        "evidence_pointers": pointers,
        "claims": [
            {
                **base_claim,
                "id": "claim-future",
                "object_value": "future",
                "status": "historical",
            },
            {**base_claim, "id": "claim-approved", "object_value": "current"},
        ],
    }

    first = builder.build(candidate, metadata)
    second = builder.build(
        candidate,
        {**metadata, "evidence_pointers": list(reversed(pointers))},
    )

    assert len(first) == 1
    assert first[0].claim_ids == ("claim-approved",)
    assert "current" in first[0].fact_text
    assert "future" not in first[0].fact_text
    assert first[0].evidence_count == 1
    assert first[0].source_refs == ("source-a",)
    assert first[0].atom_sha256 == second[0].atom_sha256

    changed_unapproved_pointer = builder.build(
        candidate,
        {
            **metadata,
            "evidence_pointers": [
                pointers[0],
                {**pointers[1], "content_hash": "c" * 64},
                pointers[2],
            ],
        },
    )
    assert first[0].atom_sha256 == changed_unapproved_pointer[0].atom_sha256

    moved_approved_pointer = builder.build(
        candidate,
        {
            **metadata,
            "evidence_pointers": [
                pointers[0],
                pointers[1],
                {**pointers[2], "observed_path": "src/moved.py"},
            ],
        },
    )
    assert first[0].atom_sha256 != moved_approved_pointer[0].atom_sha256

    mixed_candidate = {**candidate, "claim_ids": ["claim-approved", "claim-future"]}
    current_only = builder.build(mixed_candidate, metadata)
    with_history = builder.build(mixed_candidate, metadata, include_historical=True)
    assert [atom.status for atom in current_only] == ["accepted"]
    assert {atom.status for atom in with_history} == {"accepted", "historical"}
    assert "status=historical" in next(
        atom.rendered_text for atom in with_history if atom.status == "historical"
    )


def test_contested_claim_identity_is_one_bundle_across_valid_intervals() -> None:
    counter = UnicodeHeuristicTokenCounter()
    builder = AtomBuilder(counter)

    def build(memory_id: str, valid_from: str, valid_to: str) -> Any:
        claim_id = f"claim-{memory_id}"
        return builder.build(
            {
                "memory": {
                    "id": memory_id,
                    "category": "decision",
                    "status": "active",
                    "memory_type": "project",
                    "title": "Contested framework",
                    "content": "The worker framework is contested.",
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
                "score": 1.0,
                "truth_state": "contested",
                "claim_ids": [claim_id],
                "trace": {"freshness": "fresh", "evidence_count": 1},
            },
            {
                "source_refs": [memory_id],
                "evidence_pointers": [{"source_id": memory_id}],
                "claims": [
                    {
                        "id": claim_id,
                        "subject_entity_id": "project.worker",
                        "subject": "Worker",
                        "predicate": "uses_framework",
                        "object_kind": "literal",
                        "object_entity_id": None,
                        "object_name": None,
                        "object_value": memory_id,
                        "polarity": "positive",
                        "modality": "decision",
                        "qualifiers": {},
                        "status": "contested",
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                        "recorded_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            },
        )[0]

    earlier = build(
        "framework-fastapi",
        "2026-01-01T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
    )
    later = build(
        "framework-django",
        "2026-06-01T00:00:00+00:00",
        "2027-01-01T00:00:00+00:00",
    )

    assert earlier.canonical_key != later.canonical_key
    assert earlier.bundle_key == later.bundle_key


def test_structured_constraints_with_different_exact_text_never_deduplicate() -> None:
    counter = UnicodeHeuristicTokenCounter()
    builder = AtomBuilder(counter)
    claim = {
        "id": "claim-constraint",
        "subject_entity_id": "request-timeout",
        "subject": "Request timeout",
        "predicate": "must_not_exceed",
        "object_kind": "literal",
        "object_entity_id": None,
        "object_name": None,
        "object_value": 30,
        "polarity": "positive",
        "modality": "constraint",
        "qualifiers": {},
        "status": "accepted",
        "valid_from": None,
        "valid_to": None,
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }

    def build(memory_id: str, content: str) -> Any:
        return builder.build(
            {
                "memory": {
                    "id": memory_id,
                    "category": "constraint",
                    "status": "active",
                    "memory_type": "project",
                    "title": "Timeout constraint",
                    "content": content,
                    "valid_from": None,
                    "valid_to": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
                "score": 1.0,
                "truth_state": "resolved",
                "claim_ids": ["claim-constraint"],
                "trace": {"freshness": "fresh", "evidence_count": 1},
            },
            {
                "source_refs": [memory_id],
                "evidence_pointers": [{"source_id": memory_id}],
                "claims": [claim],
            },
        )[0]

    production = build("constraint-production", "Timeout must not exceed 30 seconds.")
    exception = build(
        "constraint-exception",
        "Timeout must not exceed 30 seconds, except offline migrations.",
    )
    deduplicated, _ = exact_deduplicate([production, exception], counter)

    assert len(deduplicated) == 2


@pytest.mark.v23
def test_deduplicated_fact_handle_rebuilds_all_current_evidence_in_one_explain_call(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service = msc_runtime
    content = "RefundService is the only refund entry point."
    created_memory_ids: list[str] = []
    for index in range(2):
        memory = make_memory(
            title="Refund entry decision",
            content=content,
            key=f"refund.entry.source.{index}",
            source_ref=f"manual:refund-source-{index}",
        )
        created = service.propose(
            memory.model_copy(
                update={
                    "claim_candidates": [
                        ClaimCandidate(
                            subject_hint="project.refund_service",
                            subject_type=EntityType.PROJECT,
                            predicate="is_only_refund_entry",
                            object_kind=ClaimObjectKind.LITERAL,
                            object_value=True,
                            confidence=0.9,
                            evidence_span=EvidenceSpan(
                                start=0,
                                end=len(content),
                                quote=content,
                            ),
                        )
                    ]
                }
            ),
            actor="test",
        )
        created_memory_ids.append(str(created["id"]))

    # Make the delivered primary the lexicographically larger random UUID. Rebuilding
    # every component with equal scores used to pick the smaller UUID and change the hash.
    expected_primary_memory_id = max(created_memory_ids)

    def force_nonlexicographic_primary(
        atoms: list[ContextAtom], counter: TokenCounter
    ) -> tuple[list[ContextAtom], dict[str, str]]:
        rewritten = [
            atom.model_copy(
                update={"utility": 2.0 if atom.memory_id == expected_primary_memory_id else 1.0}
            )
            for atom in atoms
        ]
        return exact_deduplicate(rewritten, counter)

    monkeypatch.setattr(
        "memoryos.context.compiler.exact_deduplicate",
        force_nonlexicographic_primary,
    )

    context = service.context(
        ContextRequest(
            task="find the only RefundService refund entry point",
            repository="repo-a",
            budget_tokens=5000,
        )
    )
    handles = re.findall(r"\[([^ ]+) @ ([0-9a-f]{64})\]", context["text"])
    assert len(handles) == 1
    memory_id, atom_hash = handles[0]
    assert memory_id == expected_primary_memory_id

    explanation = service.explain(memory_id, expected_atom_sha256=atom_hash)

    fact = explanation["sections"]["fact"][0]
    assert fact["atom_sha256"] == atom_hash
    assert len(fact["memory_ids"]) == 2
    assert len(explanation["sections"]["evidence"]) == 2

    fact_only = service.explain(
        memory_id,
        expected_atom_sha256=atom_hash,
        sections=["fact"],
    )
    assert set(fact_only["sections"]) == {"fact"}
    assert fact_only["usage"]["evidence_expansion_tokens"] == 0

    history_only = service.explain(
        memory_id,
        expected_atom_sha256=atom_hash,
        sections=["history"],
    )
    assert set(history_only["sections"]) == {"history"}
    assert history_only["usage"]["evidence_expansion_tokens"] == 0
    assert history_only["usage"]["history_expansion_tokens"] > 0

    with pytest.raises(ValueError, match="at least one explain section"):
        service.explain(
            memory_id,
            expected_atom_sha256=atom_hash,
            sections=[],
        )


@pytest.mark.v23
def test_explain_returns_only_evidence_bound_to_the_selected_atom(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    database, service = msc_runtime
    refund_quote = "RefundService owns refund processing."
    cache_quote = "CacheService owns cache invalidation."
    content = f"{refund_quote} {cache_quote}"
    memory = service.propose(
        make_memory(
            title="Service ownership facts",
            content=content,
            key="service.ownership",
        ).model_copy(
            update={
                "claim_candidates": [
                    ClaimCandidate(
                        subject_hint="RefundService",
                        subject_type=EntityType.SERVICE,
                        predicate="owns",
                        object_kind=ClaimObjectKind.LITERAL,
                        object_value="refund processing",
                        confidence=0.9,
                        evidence_span=EvidenceSpan(
                            start=0,
                            end=len(refund_quote),
                            quote=refund_quote,
                        ),
                    ),
                    ClaimCandidate(
                        subject_hint="CacheService",
                        subject_type=EntityType.SERVICE,
                        predicate="owns",
                        object_kind=ClaimObjectKind.LITERAL,
                        object_value="cache invalidation",
                        confidence=0.9,
                        evidence_span=EvidenceSpan(
                            start=len(refund_quote) + 1,
                            end=len(content),
                            quote=cache_quote,
                        ),
                    ),
                ]
            }
        ),
        actor="test",
    )
    with database.session() as session:
        claims = list(session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory["id"])))
        claim_by_subject = {str(claim.object_value): claim for claim in claims}
        for evidence in session.scalars(
            select(ClaimEvidenceRow).where(
                ClaimEvidenceRow.claim_id.in_([claim.id for claim in claims])
            )
        ):
            evidence.support_weight = (
                0.1 if evidence.claim_id == claim_by_subject["refund processing"].id else 1.0
            )

    context = service.context(
        ContextRequest(
            task="check refund processing and cache invalidation ownership",
            repository="repo-a",
            budget_tokens=5000,
        )
    )
    debug = service.debug_context(retrieval_run_id=context["retrieval_run_id"])
    refund_atom = next(
        atom
        for atom in debug["context_diagnostics"]["selected_atoms"]
        if "refund processing" in atom["fact_text"]
    )

    explanation = service.explain(
        memory["id"],
        expected_atom_sha256=refund_atom["atom_sha256"],
        sections=["evidence"],
    )

    assert {item["claim_id"] for item in explanation["sections"]["evidence"]} == {
        claim_by_subject["refund processing"].id
    }
    assert {item["excerpt"] for item in explanation["sections"]["evidence"]} == {refund_quote}


@pytest.mark.v23
def test_index_context_handle_can_expand_to_evidence_in_one_call(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    _, service = msc_runtime
    memory = service.propose(
        make_memory(
            title="Refund navigation record",
            content="RefundService owns refund processing.",
            key="refund.navigation",
        ),
        actor="test",
    )
    context = service.context(
        ContextRequest(
            task="find refund processing",
            repository="repo-a",
            budget_tokens=5000,
            detail_level=DetailLevel.INDEX,
        )
    )
    handle = re.search(rf"\[{memory['id']} @ ([0-9a-f]{{64}})\]", context["text"])
    assert handle is not None
    assert "Relevant record: Refund navigation record" in context["text"]

    explanation = service.explain(
        memory["id"],
        expected_atom_sha256=handle.group(1),
    )

    assert explanation["atom_sha256"] == handle.group(1)
    assert explanation["sections"]["evidence"]


def test_constraint_atom_preserves_negation_threshold_unit_scope_and_exception() -> None:
    counter = UnicodeHeuristicTokenCounter()
    text = "生产请求超时不得超过 30 秒, 离线迁移任务除外。"
    candidate = {
        "memory": {
            "id": "constraint-1",
            "key": "request.timeout.production",
            "category": "constraint",
            "status": "active",
            "memory_type": "project",
            "title": "Timeout",
            "content": text,
            "valid_from": None,
            "valid_to": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        "score": 1.0,
        "truth_state": "resolved",
        "trace": {"freshness": "fresh", "evidence_count": 1},
    }
    metadata = {
        "source_refs": ["policy"],
        "evidence_pointers": [{"source": "policy", "hash": "a" * 64}],
        "claims": [],
    }

    atom = AtomBuilder(counter).build(
        candidate,
        metadata,
        requested_detail=DetailLevel.FACT,
    )[0]

    assert atom.compression_policy is CompressionPolicy.PINNED
    assert text in atom.rendered_text
    assert 'write_key="request.timeout.production"' in atom.rendered_text
    for fragment in ("不得", "30", "秒", "生产请求", "除外"):
        assert fragment in atom.rendered_text


def test_decision_atom_keeps_confirmed_memory_when_structured_claim_loses_version() -> None:
    counter = UnicodeHeuristicTokenCounter()
    content = "生产数据库当前版本为 PostgreSQL 18; PostgreSQL 17 已被替换。"
    candidate = {
        "memory": {
            "id": "database-version-18",
            "key": "database.production-postgres-version",
            "category": "decision",
            "status": "active",
            "memory_type": "project",
            "title": "Production database version",
            "content": content,
            "valid_from": None,
            "valid_to": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        "score": 1.0,
        "truth_state": "resolved",
        "claim_ids": ["database-claim"],
        "trace": {"freshness": "fresh", "evidence_count": 1},
    }
    metadata = {
        "source_refs": ["conversation"],
        "evidence_pointers": [{"claim_id": "database-claim", "source_id": "source"}],
        "claims": [
            {
                "id": "database-claim",
                "subject_entity_id": "project.production_database",
                "subject": "project.production_database",
                "predicate": "uses",
                "object_kind": "literal",
                "object_entity_id": None,
                "object_name": None,
                "object_value": "postgresql",
                "polarity": "positive",
                "modality": "decision",
                "qualifiers": {},
                "status": "accepted",
                "valid_from": None,
                "valid_to": None,
                "recorded_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }

    atom = AtomBuilder(counter).build(candidate, metadata)[0]

    assert atom.fact_text == "project.production_database uses postgresql"
    assert f"confirmed_memory={canonical_json(content)}" in atom.rendered_text
    assert 'write_key="database.production-postgres-version"' in atom.rendered_text


@pytest.mark.v23
def test_shadow_mode_returns_legacy_contract_and_persists_compact_diagnostics(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    settings = settings_for(tmp_path / "shadow-data", context_compiler_mode="msc_shadow")
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    try:
        service.propose(make_memory(), actor="test")
        response = service.context(ContextRequest(task="FastAPI", repository="repo-a"))

        assert "schema_version" not in response
        assert {"query_plan", "sections", "manifest", "debug"}.issubset(response)
        with database.session() as session:
            run = session.get(RetrievalRunRow, response["retrieval_run_id"])
            assert run is not None
            assert run.context_shadow_json["payload"]["schema_version"] == "2.3"
            assert run.context_usage_json["counter_kind"] == "estimated"
    finally:
        database.close()


@pytest.mark.v23
def test_unexpected_shadow_failure_never_breaks_the_legacy_response(
    tmp_path: Path,
    make_memory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(tmp_path / "shadow-failure", context_compiler_mode="msc_shadow")
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)

    def fail_shadow(_request: ContextRequest, _legacy: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("fixture shadow failure")

    monkeypatch.setattr(service.context_builder, "_build_msc", fail_shadow)
    try:
        service.propose(make_memory(), actor="test")
        response = service.context(ContextRequest(task="FastAPI", repository="repo-a"))

        assert "schema_version" not in response
        assert "FastAPI" in response["text"]
        with database.session() as session:
            run = session.get(RetrievalRunRow, response["retrieval_run_id"])
            assert run is not None
            assert run.context_shadow_json["error"]["code"] == "MSC_SHADOW_FAILURE"
            assert run.context_shadow_json["error"]["details"] == {"exception_type": "RuntimeError"}
    finally:
        database.close()


@pytest.mark.v23
def test_delta_rebases_when_budget_policy_changes(
    msc_runtime: tuple[Database, MemoryService],
    make_memory: Any,
) -> None:
    _, service = msc_runtime
    service.propose(make_memory(), actor="test")
    first = service.context(
        ContextRequest(
            task="FastAPI decision",
            repository="repo-a",
            budget_profile=BudgetProfile.SMALL,
        )
    )

    rebased = service.context(
        ContextRequest(
            task="FastAPI decision",
            repository="repo-a",
            budget_profile=BudgetProfile.LARGE,
            previous_context_id=first["context_id"],
            response_mode="delta",
        )
    )

    assert rebased["mode"] == "full"
    assert rebased["fallback_reason"] == "policy_mismatch"
