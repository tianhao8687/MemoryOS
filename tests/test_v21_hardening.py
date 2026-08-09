from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from memoryos.claims.predicates import classify_claim_values
from memoryos.claims.truth import TruthMaintenanceService
from memoryos.consolidation import ConsolidationService
from memoryos.db.models import (
    ClaimVersionRow,
    MemoryHealthRow,
    MemoryRow,
    PossibleConflictRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimObjectKind,
    ClaimPolarity,
    ConflictStrategy,
    ConsolidateRequest,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    EvidenceSpan,
    MemoryCreate,
    MemoryTemperature,
    MemoryType,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.errors import InvalidTransitionError, ProviderError
from memoryos.evaluation import CodingMemoryBench
from memoryos.providers.base import ProviderMetadata
from memoryos.retrieval.search import RetrievalEngine


def _migrate(database: Database, target: str) -> None:
    migrations = Path(__file__).resolve().parents[1] / "memoryos" / "db" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, target)


@pytest.mark.v21
def test_a34_immutable_migration_replays_v2_data(database: Database) -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "memoryos"
        / "db"
        / "migrations"
        / "versions"
        / "0002_memory_intelligence.py"
    ).read_text(encoding="utf-8")
    assert "Base.metadata" not in migration
    assert "create_all" not in migration

    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[1] / "memoryos" / "db" / "migrations"),
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_initial")
    now = datetime.now(UTC)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO memories (id,scope_type,scope_key,memory_type,category,title,content,"
            "status,confidence,importance,created_at,updated_at,created_by,sensitivity,"
            "metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "m-v1",
                "repository",
                "repo-migration",
                "project",
                "decision",
                "Preserved V1 memory",
                "The production database uses SQLite.",
                "active",
                0.9,
                0.8,
                now,
                now,
                "manual",
                "normal",
                "{}",
            ),
        )
    _migrate(database, "0002_memory_intelligence")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO entities (id,scope_type,scope_key,entity_type,canonical_name,"
            "normalized_name,aliases_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "e-v2",
                "repository",
                "repo-migration",
                "project",
                "project.production_database",
                "project.production_database",
                "[]",
                now,
                now,
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO claims (id,memory_id,subject_entity_id,predicate,object_kind,"
            "object_value,polarity,modality,qualifiers_json,canonical_key,confidence,status,"
            "recorded_at,stale_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c-v2",
                "m-v1",
                "e-v2",
                "uses",
                "literal",
                '"sqlite"',
                "positive",
                "decision",
                "{}",
                "project.production_database|uses|sqlite|positive",
                0.9,
                "accepted",
                now,
                "fresh",
            ),
        )
    _migrate(database, "head")
    with database.session() as session:
        version = session.scalar(select(ClaimVersionRow).where(ClaimVersionRow.claim_id == "c-v2"))
        assert version is not None
        assert version.object_value == "sqlite"
    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option(
            "script_location",
            str(Path(__file__).resolve().parents[1] / "memoryos" / "db" / "migrations"),
        )
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_memory_intelligence")
    _migrate(database, "head")
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(ClaimVersionRow)) == 1


@pytest.mark.v21
def test_a35_a36_claim_versions_reconstruct_transaction_time(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    candidate = service.propose(
        make_memory(created_by=CreatedBy.AGENT, activate_immediately=False), actor="agent"
    )
    service.confirm(candidate["id"], actor="reviewer")
    with database.session() as session:
        versions = list(
            session.scalars(
                select(ClaimVersionRow)
                .where(ClaimVersionRow.memory_id == candidate["id"])
                .order_by(ClaimVersionRow.version_number)
            )
        )
        assert [row.status.value for row in versions] == ["candidate", "accepted"]
        assert versions[0].transaction_to == versions[1].transaction_from
        known_after_acceptance = versions[1].transaction_from + timedelta(microseconds=1)
    service.forget(candidate["id"], actor="reviewer")
    historical = service.current_truth(
        CurrentTruthRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            subject="project.backend_framework",
            predicate="uses",
            as_known_at=known_after_acceptance,
        )
    )
    assert historical["state"] == "resolved"
    assert historical["accepted_claims"][0]["memory_id"] == candidate["id"]
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ClaimVersionRow)
                .where(ClaimVersionRow.memory_id == candidate["id"])
            )
            == 3
        )


class _RelationshipJudge:
    calls = 0

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("fixture", "relationship-v1", True, 4000, ("abstain",))

    def judge(
        self, left: dict[str, Any], right: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        del left, right, evidence
        self.calls += 1
        return {
            "relationship": "contradicts",
            "confidence": 0.91,
            "explanation": "Bounded fixture judgement",
            "abstain": False,
        }


class _FailingRelationshipJudge(_RelationshipJudge):
    def judge(
        self, left: dict[str, Any], right: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        del left, right, evidence
        self.calls += 1
        raise ProviderError("relationship provider unavailable")


def _explicit_memory(value: str, *, active: bool) -> MemoryCreate:
    evidence = f"The repository constraint mentions {value}."
    return MemoryCreate(
        scope_type=ScopeType.REPOSITORY,
        scope_key="repo-router",
        memory_type=MemoryType.PROJECT,
        category="constraint",
        title=f"Constraint {value}",
        content=evidence,
        created_by=CreatedBy.MANUAL if active else CreatedBy.AGENT,
        activate_immediately=active,
        source=SourceCreate(
            source_type=SourceType.MANUAL if active else SourceType.AGENT,
            source_ref=f"fixture:{value}",
            excerpt=evidence,
        ),
        claim_candidates=[
            ClaimCandidate(
                subject_hint="project.dependencies",
                subject_type=EntityType.PROJECT,
                predicate="forbidden",
                object_kind=ClaimObjectKind.LITERAL,
                object_value=value,
                confidence=0.9,
                evidence_span=EvidenceSpan(start=0, end=len(evidence), quote=evidence),
            )
        ],
    )


@pytest.mark.v21
def test_a37_a39_model_judge_only_receives_uncertain_pairs(
    database: Database, service: MemoryService
) -> None:
    current = service.propose(_explicit_memory("redis", active=True), actor="fixture")
    candidate = service.propose(_explicit_memory("celery", active=False), actor="fixture")
    judge = _RelationshipJudge()
    truth = TruthMaintenanceService(judge)
    with database.session() as session:
        memory = session.get(MemoryRow, candidate["id"])
        assert memory is not None
        conflicts = truth.find_semantic_conflict_memories(session, memory)
        assert [item.id for item in conflicts] == [current["id"]]
        queue = session.scalar(select(PossibleConflictRow))
        assert queue is not None
        assert queue.status.value == "confirmed"
        assert queue.provider_fingerprint and queue.prompt_version and queue.evidence_hash
    assert judge.calls == 1
    service.confirm(
        candidate["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        actor="reviewer",
    )
    resolved = service.current_truth(
        CurrentTruthRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-router",
            subject="project.dependencies",
            predicate="forbidden",
        )
    )
    assert resolved["state"] == "contested"
    assert {item["object_value"] for item in resolved["conflicting_claims"]} == {
        "redis",
        "celery",
    }
    obvious = classify_claim_values(
        left_subject="project.production_database",
        left_predicate="uses",
        left_object="sqlite",
        left_polarity=ClaimPolarity.POSITIVE,
        right_subject="project.production_database",
        right_predicate="uses",
        right_object="postgresql",
        right_polarity=ClaimPolarity.POSITIVE,
    )
    assert obvious.relationship == "contradicts"
    assert obvious.model_eligible is False


@pytest.mark.v21
def test_a39_provider_failure_abstains_without_mutating_truth(
    database: Database, service: MemoryService
) -> None:
    current = service.propose(_explicit_memory("redis", active=True), actor="fixture")
    candidate = service.propose(_explicit_memory("celery", active=False), actor="fixture")
    truth = TruthMaintenanceService(_FailingRelationshipJudge())
    with database.session() as session:
        memory = session.get(MemoryRow, candidate["id"])
        assert memory is not None
        assert truth.find_semantic_conflict_memories(session, memory) == []
        queue = session.scalar(select(PossibleConflictRow))
        assert queue is not None
        assert queue.status.value == "abstained"
        current_claim = session.scalar(
            select(ClaimVersionRow).where(
                ClaimVersionRow.memory_id == current["id"],
                ClaimVersionRow.transaction_to.is_(None),
            )
        )
        candidate_claim = session.scalar(
            select(ClaimVersionRow).where(
                ClaimVersionRow.memory_id == candidate["id"],
                ClaimVersionRow.transaction_to.is_(None),
            )
        )
        assert current_claim is not None and current_claim.status.value == "accepted"
        assert candidate_claim is not None and candidate_claim.status.value == "candidate"


class _EmbeddingProvider:
    name = "fixture"
    model = "three-dimensional"

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(self.name, self.model, False, 1000, ("embedding",))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        if "FastAPI" in text or "semanticneedle" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]


@pytest.mark.v21
def test_a40_a41_ann_is_live_and_exact_fallback_is_explicit(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    memory = service.propose(make_memory(), actor="fixture")
    provider = _EmbeddingProvider()
    engine = RetrievalEngine(database, provider)
    assert engine.index_memory(memory["id"])
    result = engine.search(SearchRequest(query="semanticneedle", limit=5))
    assert result["items"][0]["memory"]["id"] == memory["id"]
    assert result["mode"] == "hybrid-ann"
    assert engine.vector_status()[0]["status"] == "ready"
    engine.close()

    restarted = RetrievalEngine(database, provider)
    restarted_result = restarted.search(SearchRequest(query="semanticneedle", limit=5))
    assert restarted_result["items"][0]["memory"]["id"] == memory["id"]
    assert restarted_result["mode"] == "hybrid-ann"
    restarted.close()

    database.settings.ann_enabled = False
    fallback = RetrievalEngine(database, provider)
    result = fallback.search(SearchRequest(query="semanticneedle", limit=5))
    fallback.close()
    assert result["items"][0]["memory"]["id"] == memory["id"]
    assert "exact-fallback" in result["mode"]


@pytest.mark.v21
def test_a45_blind_runtime_rejects_nested_gold_fields() -> None:
    with pytest.raises(AssertionError, match="leaked a gold field"):
        CodingMemoryBench._blind_execute(
            [{"id": "leak", "payload": {"expected": "secret"}}],
            lambda case: case,
        )


class _InvalidGroundingJudge:
    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("fixture", "consolidation-v1", True, 4000, ("abstain",))

    def judge(self, episodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "status": "candidate",
            "proposal": "Always use pnpm.",
            "supporting_memory_ids": [
                *[str(item["memory_id"]) for item in episodes],
                "hallucinated-memory-id",
            ],
            "counterevidence_memory_ids": [],
            "confidence": 0.99,
        }


@pytest.mark.v21
def test_a49_a50_invalid_model_grounding_falls_back_to_candidate(
    database: Database, service: MemoryService
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    for index in range(3):
        content = "We prefer pnpm for package management."
        service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key="repo-grounding",
                memory_type=MemoryType.EPISODIC,
                category="preference",
                title=f"Package manager episode {index}",
                content=content,
                created_by=CreatedBy.MANUAL,
                activate_immediately=True,
                source=SourceCreate(
                    source_type=SourceType.CONVERSATION,
                    source_ref=f"grounding:{index}",
                    captured_at=start + timedelta(days=index * 7),
                    excerpt=content,
                ),
            ),
            actor="fixture",
        )
    consolidation = ConsolidationService(database, _InvalidGroundingJudge())
    result = consolidation.propose(
        ConsolidateRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-grounding",
        )
    )
    assert result["count"] == 1
    proposal = result["proposals"][0]
    assert proposal["status"] == "candidate"
    assert proposal["proposal"]["abstraction_mode"] == "offline-extractive-fallback"
    assert proposal["proposal"]["activation"] == "human_confirmation_required"


@pytest.mark.v21
def test_a51_archive_is_reversible_and_protects_sole_truth(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    accepted = service.propose(make_memory(), actor="fixture")
    service.evaluate_memory_health()
    with pytest.raises(InvalidTransitionError, match="sole accepted"):
        service.archive_memory(accepted["id"], actor="fixture")

    candidate = service.propose(
        make_memory(
            title="Cold candidate",
            content="A low-value candidate note.",
            key="cold.candidate",
            created_by=CreatedBy.AGENT,
            activate_immediately=False,
        ),
        actor="fixture",
    )
    service.evaluate_memory_health()
    with database.session() as session:
        health = session.get(MemoryHealthRow, candidate["id"])
        assert health is not None
        health.temperature = MemoryTemperature.COLD
    archived = service.archive_memory(candidate["id"], actor="fixture")
    restored = service.restore_archived_memory(candidate["id"], actor="fixture")
    assert archived["temperature"] == "archived"
    assert restored["temperature"] == "cold"
