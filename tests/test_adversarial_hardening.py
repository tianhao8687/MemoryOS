from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from memoryos.api import create_app
from memoryos.backup import BackupService
from memoryos.config import settings_for
from memoryos.consolidation import ConsolidationService
from memoryos.db.models import (
    AnnIndexStateRow,
    AuditEventRow,
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    EmbeddingRow,
    MemoryHealthRow,
    MemoryRow,
    PossibleConflictRow,
    RetrievalRunRow,
    SourceAnchorRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimObjectKind,
    ClaimPolarity,
    ConflictStrategy,
    ConsolidateRequest,
    ContextRequest,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    EvidenceSpan,
    FreshnessState,
    MemoryCreate,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    ScopeType,
    SearchRequest,
    Sensitivity,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.errors import BackupError, ConflictDetectedError, InvalidTransitionError
from memoryos.freshness.git_compare import compare_anchor
from memoryos.integrations.git import discover_git_context
from memoryos.providers.base import ProviderMetadata
from memoryos.retrieval.search import RetrievalEngine
from memoryos.security.token import TokenManager


def _utc_for_test(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _explicit_memory(
    value: str,
    *,
    scope_key: str,
    subject: str = "project.production_database",
    predicate: str = "uses",
    valid_from: datetime | None = None,
    captured_at: datetime | None = None,
    title_suffix: str = "",
    activate: bool = True,
    polarity: ClaimPolarity = ClaimPolarity.POSITIVE,
    memory_type: MemoryType = MemoryType.PROJECT,
) -> MemoryCreate:
    evidence = f"The repository states that {subject} {predicate} {value}."
    return MemoryCreate(
        scope_type=ScopeType.REPOSITORY,
        scope_key=scope_key,
        memory_type=memory_type,
        category="decision",
        key=f"audit.{value}.{title_suffix or 'current'}",
        title=f"{predicate} {value}{title_suffix}",
        content=evidence,
        valid_from=valid_from,
        created_by=CreatedBy.MANUAL if activate else CreatedBy.AGENT,
        activate_immediately=activate,
        source=SourceCreate(
            source_type=SourceType.MANUAL if activate else SourceType.AGENT,
            source_ref=f"audit:{scope_key}:{value}:{title_suffix}",
            captured_at=captured_at or datetime.now(UTC),
            excerpt=evidence,
        ),
        claim_candidates=[
            ClaimCandidate(
                subject_hint=subject,
                subject_type=EntityType.PROJECT,
                predicate=predicate,
                object_kind=ClaimObjectKind.LITERAL,
                object_value=value,
                polarity=polarity,
                confidence=0.9,
                evidence_span=EvidenceSpan(start=0, end=len(evidence), quote=evidence),
            )
        ],
    )


@pytest.mark.v21
def test_graph_candidates_cannot_reintroduce_future_memory(service: MemoryService) -> None:
    future = service.propose(
        _explicit_memory(
            "postgresql",
            scope_key="future-leak",
            valid_from=datetime.now(UTC) + timedelta(days=1),
        ),
        actor="audit",
    )
    result = service.search(SearchRequest(query="postgresql", scope_key="future-leak", limit=10))
    assert future["id"] not in {item["memory"]["id"] for item in result["items"]}


@pytest.mark.v21
def test_future_support_does_not_allow_archiving_current_truth(service: MemoryService) -> None:
    current = service.propose(
        _explicit_memory("postgresql", scope_key="archive-truth"), actor="audit"
    )
    service.propose(
        _explicit_memory(
            "postgresql",
            scope_key="archive-truth",
            valid_from=datetime.now(UTC) + timedelta(days=7),
            title_suffix=" future",
        ),
        actor="audit",
    )
    with pytest.raises(InvalidTransitionError, match="sole accepted"):
        service.archive_memory(current["id"], actor="audit")


@pytest.mark.v21
def test_as_known_at_reconstructs_archive_and_restore_visibility(
    database: Database, service: MemoryService
) -> None:
    first = service.propose(
        _explicit_memory("postgresql", scope_key="archive-history", title_suffix=" first"),
        actor="audit",
    )
    second = service.propose(
        _explicit_memory("postgresql", scope_key="archive-history", title_suffix=" second"),
        actor="audit",
    )
    request = CurrentTruthRequest(
        scope_type=ScopeType.REPOSITORY,
        scope_key="archive-history",
        subject="project.production_database",
        predicate="uses",
    )

    service.archive_memory(first["id"], actor="audit")
    with database.session() as session:
        archive_time = session.scalar(
            select(AuditEventRow.timestamp).where(
                AuditEventRow.entity_id == first["id"],
                AuditEventRow.action == "health_archive",
            )
        )
    assert archive_time is not None
    before_archive = _utc_for_test(archive_time) - timedelta(microseconds=1)
    during_archive = _utc_for_test(archive_time) + timedelta(microseconds=1)
    historical_before = service.current_truth(
        request.model_copy(update={"as_known_at": before_archive})
    )
    historical_during = service.current_truth(
        request.model_copy(update={"as_known_at": during_archive})
    )
    assert {item["memory_id"] for item in historical_before["accepted_claims"]} == {
        first["id"],
        second["id"],
    }
    assert {item["memory_id"] for item in historical_during["accepted_claims"]} == {second["id"]}

    service.restore_archived_memory(first["id"], actor="audit")
    current = service.current_truth(request)
    assert {item["memory_id"] for item in current["accepted_claims"]} == {
        first["id"],
        second["id"],
    }


@pytest.mark.v21
def test_direct_active_proposal_expires_due_truth_before_conflict_check(
    database: Database, service: MemoryService
) -> None:
    expired = service.propose(
        _explicit_memory("sqlite", scope_key="proposal-expiry"), actor="audit"
    )
    with database.session() as session:
        row = session.get(MemoryRow, expired["id"])
        assert row is not None
        row.ttl_seconds = 1
        row.created_at = datetime.now(UTC) - timedelta(minutes=1)

    replacement = service.propose(
        _explicit_memory("postgresql", scope_key="proposal-expiry"), actor="audit"
    )

    assert replacement["status"] == "active"
    assert service.get(expired["id"])["status"] == "expired"


@pytest.mark.v21
def test_overlapping_future_active_truths_still_conflict(service: MemoryService) -> None:
    future = datetime.now(UTC) + timedelta(days=7)
    service.propose(
        _explicit_memory("sqlite", scope_key="future-conflict", valid_from=future),
        actor="audit",
    )
    with pytest.raises(ConflictDetectedError):
        service.propose(
            _explicit_memory("postgresql", scope_key="future-conflict", valid_from=future),
            actor="audit",
        )


@pytest.mark.v21
def test_source_anchor_freshness_appends_claim_version(
    tmp_path: Path, database: Database, service: MemoryService
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text("def backend():\n    return 'fastapi'\n", encoding="utf-8")
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "audit@example.invalid"],
        ["git", "config", "user.name", "MemoryOS Audit"],
        ["git", "add", "service.py"],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(  # noqa: S603 - fixed git commands against a temporary fixture repo
            args, cwd=repo, check=True, capture_output=True, text=True
        )
    memory = service.propose(_explicit_memory("fastapi", scope_key="anchor-version"), actor="audit")
    with database.session() as session:
        before = int(
            session.scalar(
                select(func.count())
                .select_from(ClaimVersionRow)
                .where(ClaimVersionRow.memory_id == memory["id"])
            )
            or 0
        )
    service.create_source_anchor(
        memory_id=memory["id"], repository_path=str(repo), path="service.py"
    )
    with database.session() as session:
        claim = session.scalar(select(ClaimRow).where(ClaimRow.memory_id == memory["id"]))
        version = session.scalar(
            select(ClaimVersionRow).where(
                ClaimVersionRow.memory_id == memory["id"],
                ClaimVersionRow.transaction_to.is_(None),
            )
        )
        after = int(
            session.scalar(
                select(func.count())
                .select_from(ClaimVersionRow)
                .where(ClaimVersionRow.memory_id == memory["id"])
            )
            or 0
        )
        assert claim is not None and version is not None
        assert claim.stale_state.value == version.stale_state.value == "fresh"
        assert after == before + 1


@pytest.mark.v21
def test_imported_anchor_path_cannot_read_outside_repository(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    secret = "outside-repository-secret"
    outside.write_text(secret, encoding="utf-8")
    repo = tmp_path / "anchor-repo"
    repo.mkdir()
    (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "audit@example.invalid"],
        ["git", "config", "user.name", "MemoryOS Audit"],
        ["git", "add", "tracked.txt"],
        ["git", "commit", "-m", "fixture"],
    ):
        subprocess.run(  # noqa: S603 - fixed git commands against a temporary fixture repo
            args, cwd=repo, check=True, capture_output=True, text=True
        )
    context = discover_git_context(repo)
    anchor = SourceAnchorRow(
        repository_stable_key=context.stable_key,
        commit_sha=context.head,
        path="../secret.txt",
        blob_sha="0" * 40,
        evidence_excerpt=secret,
        excerpt_hash=hashlib.sha256(secret.encode()).hexdigest(),
        context_hash=hashlib.sha256(secret.encode()).hexdigest(),
        freshness_state=FreshnessState.FRESH,
        cached_head=context.head,
        checked_at=datetime.now(UTC),
        metadata_json={},
    )

    result = compare_anchor(anchor, repo)

    assert result.state is FreshnessState.UNKNOWN
    assert result.current_excerpt is None
    assert secret not in result.explanation


@pytest.mark.v21
def test_manual_possible_conflict_resolution_updates_live_truth(
    database: Database, service: MemoryService
) -> None:
    service.propose(
        _explicit_memory(
            "redis",
            scope_key="manual-conflict",
            subject="project.dependencies",
            predicate="forbidden",
        ),
        actor="audit",
    )
    candidate = service.propose(
        _explicit_memory(
            "celery",
            scope_key="manual-conflict",
            subject="project.dependencies",
            predicate="forbidden",
            activate=False,
        ),
        actor="audit",
    )
    service.confirm(candidate["id"], actor="audit")
    with database.session() as session:
        queue = session.scalar(select(PossibleConflictRow))
        assert queue is not None
        queue_id = queue.id
    service.resolve_possible_conflict(
        queue_id, confirmed=True, actor="audit", rationale="manually confirmed"
    )
    truth = service.current_truth(
        CurrentTruthRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="manual-conflict",
            subject="project.dependencies",
            predicate="forbidden",
        )
    )
    with database.session() as session:
        statuses = {row.status.value for row in session.scalars(select(ClaimRow))}
        relations = int(session.scalar(select(func.count()).select_from(ClaimRelationRow)) or 0)
    assert truth["state"] == "contested"
    assert statuses == {"contested"}
    assert relations == 1


@pytest.mark.v21
def test_cross_port_origin_cannot_reuse_ui_cookie(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "csrf")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
        for origin in (
            "http://127.0.0.1:9999",
            "http://127.0.0.1",
            "http://localhost:8766",
            "http://127.0.0.1:8765/",
            "http://127.0.0.1:8765?cross-port=true",
            "http://[::1",
        ):
            response = client.post("/api/backup", headers={"Origin": origin})
            assert response.status_code == 403


@pytest.mark.v21
def test_stateful_read_requires_token_before_recording_usage(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "stateful-read")
    app = create_app(settings)
    token = TokenManager(settings.token_path).get_or_create()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        created = client.post(
            "/api/memories",
            headers=headers,
            json=_explicit_memory("fastapi", scope_key="stateful-read").model_dump(mode="json"),
        ).json()["memory"]
        denied = client.get("/api/memories", params={"q": "fastapi"})
        with app.state.database.session() as session:
            assert int(session.scalar(select(func.count()).select_from(RetrievalRunRow)) or 0) == 0
            assert session.get(MemoryHealthRow, created["id"]) is None
        allowed = client.get("/api/memories", params={"q": "fastapi"}, headers=headers)
    assert denied.status_code == 401
    assert allowed.status_code == 200


class _FixtureEmbedding:
    name = "fixture"
    model = "audit-3d"

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(self.name, self.model, False, 1000, ("embedding",))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


class _MixedPolarityConsolidationJudge:
    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            "fixture", "mixed-polarity", True, 12000, ("consolidation_judgement",)
        )

    def judge(self, episodes: list[dict[str, Any]]) -> dict[str, Any]:
        positive = [
            str(item["memory_id"]) for item in episodes if item["claim"]["polarity"] == "positive"
        ]
        negative = next(
            str(item["memory_id"]) for item in episodes if item["claim"]["polarity"] == "negative"
        )
        return {
            "status": "candidate",
            "proposal": "The project always prefers pnpm.",
            "supporting_memory_ids": [positive[0], positive[1], negative],
            "counterevidence_memory_ids": [],
            "confidence": 0.99,
        }


@pytest.mark.v21
def test_ann_runtime_failure_falls_back_to_exact(
    database: Database, service: MemoryService
) -> None:
    memory = service.propose(_explicit_memory("fastapi", scope_key="ann-fallback"), actor="audit")
    engine = RetrievalEngine(database, _FixtureEmbedding())
    try:
        assert engine.index_memory(memory["id"])
        with database.session() as session:
            assert int(session.scalar(select(func.count()).select_from(EmbeddingRow)) or 0) == 1
        index = next(iter(engine._ann_indexes.values()))
        index.close()
        with database.session() as session:
            scores, mode = engine._semantic_search(session, [1.0, 0.0, 0.0], limit=10)
        assert mode == "exact-fallback"
        assert memory["id"] in scores
    finally:
        engine.close()


@pytest.mark.v21
def test_empty_embedding_response_does_not_turn_committed_write_into_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyEmbeddingResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, Any]:
            return {"data": []}

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: EmptyEmbeddingResponse())
    settings = settings_for(
        tmp_path / "empty-embedding",
        embedding_base_url="http://fixture.invalid/v1",
        embedding_model="fixture-empty",
    )
    app = create_app(settings)
    token = TokenManager(settings.token_path).get_or_create()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/memories",
            headers={"Authorization": f"Bearer {token}"},
            json=_explicit_memory("empty-vector", scope_key="provider").model_dump(mode="json"),
        )
        with app.state.database.session() as session:
            count = int(session.scalar(select(func.count()).select_from(MemoryRow)) or 0)
    assert response.status_code == 200
    assert count == 1


@pytest.mark.v21
def test_candidate_update_rejects_inverted_valid_interval(service: MemoryService) -> None:
    candidate = service.propose(
        _explicit_memory("fastapi", scope_key="update-validity", activate=False), actor="audit"
    )
    start = datetime.now(UTC) + timedelta(days=2)
    with pytest.raises(ValueError, match="valid_to must be later"):
        service.update(
            candidate["id"],
            MemoryUpdate(valid_from=start, valid_to=start - timedelta(days=1)),
            actor="audit",
        )


@pytest.mark.v21
def test_consolidation_keeps_polarity_out_of_supporting_evidence(
    service: MemoryService,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    positive_ids = []
    for index in range(3):
        memory = service.propose(
            _explicit_memory(
                "pnpm",
                scope_key="polarity-consolidation",
                subject="project.package_manager",
                predicate="prefers",
                captured_at=start + timedelta(days=index * 7),
                title_suffix=f" positive {index}",
                memory_type=MemoryType.EPISODIC,
            ),
            actor="audit",
        )
        positive_ids.append(memory["id"])
    negative = service.propose(
        _explicit_memory(
            "pnpm",
            scope_key="polarity-consolidation",
            subject="project.package_manager",
            predicate="prefers",
            captured_at=start + timedelta(days=21),
            title_suffix=" negative",
            polarity=ClaimPolarity.NEGATIVE,
            memory_type=MemoryType.EPISODIC,
            activate=False,
        ),
        actor="audit",
    )
    service.confirm(
        negative["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        actor="audit",
        rationale="retain explicit counterevidence",
    )
    result = service.consolidate(
        ConsolidateRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="polarity-consolidation",
        )
    )
    proposal = result["proposals"][0]
    assert proposal["status"] == "contested"
    assert proposal["proposal"]["polarity"] == "positive"
    assert proposal["proposal"]["supporting_episodes"] == 3
    assert set(proposal["source_memory_ids"]) == set(positive_ids)
    assert negative["id"] in {item["memory_id"] for item in proposal["counterevidence"]}


@pytest.mark.v21
def test_model_consolidation_cannot_mix_counterevidence_into_support(
    database: Database, service: MemoryService
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    positive_ids = []
    for index in range(3):
        positive_ids.append(
            service.propose(
                _explicit_memory(
                    "pnpm",
                    scope_key="model-polarity-consolidation",
                    subject="project.package_manager",
                    predicate="prefers",
                    captured_at=start + timedelta(days=index * 7),
                    title_suffix=f" model positive {index}",
                    memory_type=MemoryType.EPISODIC,
                ),
                actor="audit",
            )["id"]
        )
    negative = service.propose(
        _explicit_memory(
            "pnpm",
            scope_key="model-polarity-consolidation",
            subject="project.package_manager",
            predicate="prefers",
            captured_at=start + timedelta(days=21),
            title_suffix=" model negative",
            polarity=ClaimPolarity.NEGATIVE,
            memory_type=MemoryType.EPISODIC,
            activate=False,
        ),
        actor="audit",
    )
    service.confirm(
        negative["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        actor="audit",
        rationale="retain explicit counterevidence",
    )

    result = ConsolidationService(database, _MixedPolarityConsolidationJudge()).propose(
        ConsolidateRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="model-polarity-consolidation",
        )
    )

    proposal = result["proposals"][0]
    assert proposal["status"] == "contested"
    assert set(proposal["source_memory_ids"]) == set(positive_ids)
    assert negative["id"] in {item["memory_id"] for item in proposal["counterevidence"]}


@pytest.mark.v21
def test_context_usage_counts_only_memories_that_fit_budget(service: MemoryService) -> None:
    memory_ids = []
    for index in range(8):
        payload = _explicit_memory(
            f"framework-{index}",
            scope_key="context-usage",
            subject=f"project.component_{index}",
            predicate="uses",
            title_suffix=" " + ("x" * 40),
        )
        memory_ids.append(service.propose(payload, actor="audit")["id"])
    context = service.context(
        ContextRequest(task="framework", repository="context-usage", budget=500)
    )
    included = {item["memory_id"] for item in context["manifest"] if item["included"]}
    excluded = set(memory_ids) - included
    with service.database.session() as session:
        retrieved = {
            row.memory_id: row.retrieval_count
            for row in session.scalars(
                select(MemoryHealthRow).where(MemoryHealthRow.memory_id.in_(memory_ids))
            )
        }
    assert included
    assert excluded
    assert all(retrieved.get(memory_id) == 1 for memory_id in included)
    assert all(memory_id not in retrieved for memory_id in excluded)
    assert context["characters_used"] == len(context["text"])
    assert context["characters_used"] <= context["budget"]
    assert context["budget_exceeded"] is False


@pytest.mark.v21
def test_contested_context_group_is_atomic_and_cannot_overrun_budget(
    service: MemoryService,
) -> None:
    oversized = "budgetattack " * 80
    first_payload = _explicit_memory("postgresql", scope_key="context-contested-budget")
    second_payload = _explicit_memory(
        "mysql",
        scope_key="context-contested-budget",
        activate=False,
    )
    first = service.propose(
        first_payload.model_copy(update={"content": oversized}),
        actor="audit",
    )
    second = service.propose(
        second_payload.model_copy(update={"content": oversized}),
        actor="audit",
    )
    service.confirm(
        second["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        actor="audit",
        rationale="retain both sides for the budget attack",
    )

    context = service.context(
        ContextRequest(
            task="budgetattack database",
            repository="context-contested-budget",
            budget=500,
        )
    )

    included = {item["memory_id"] for item in context["manifest"] if item["included"]}
    contested_ids = {first["id"], second["id"]}
    assert included.isdisjoint(contested_ids) or contested_ids.issubset(included)
    assert context["characters_used"] <= context["budget"]
    assert context["budget_exceeded"] is False


@pytest.mark.v21
def test_scope_filter_is_applied_before_fts_candidate_cutoff(
    database: Database, service: MemoryService
) -> None:
    with database.session() as session:
        target = MemoryRow(
            scope_type=ScopeType.REPOSITORY,
            scope_key="wanted-repo",
            memory_type=MemoryType.PROJECT,
            category="decision",
            key="wanted",
            title="Wanted record",
            content=" ".join(["filler"] * 200 + ["crowdoutneedle"]),
            status=MemoryStatus.ACTIVE,
            confidence=0.9,
            importance=0.9,
            created_by=CreatedBy.IMPORT,
            sensitivity=Sensitivity.NORMAL,
            metadata_json={},
        )
        session.add(target)
        session.flush()
        target_id = target.id
        session.add_all(
            [
                MemoryRow(
                    scope_type=ScopeType.REPOSITORY,
                    scope_key=f"noise-{index}",
                    memory_type=MemoryType.PROJECT,
                    category="crowdoutneedle",
                    subject="crowdoutneedle",
                    key=f"crowdoutneedle.noise.{index}",
                    title=f"crowdoutneedle {index}",
                    content="crowdoutneedle",
                    status=MemoryStatus.ACTIVE,
                    confidence=0.9,
                    importance=0.9,
                    created_by=CreatedBy.IMPORT,
                    sensitivity=Sensitivity.NORMAL,
                    metadata_json={},
                )
                for index in range(1000)
            ]
        )
    with database.session() as session:
        ranked_ids = [
            str(row.memory_id)
            for row in session.execute(
                text(
                    "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH "
                    "'crowdoutneedle' ORDER BY bm25(memory_fts, 0.0, 5.0, 2.5, 1.0, 2.0, 1.0)"
                )
            )
        ]
    assert ranked_ids.index(target_id) >= 640
    result = service.search(SearchRequest(query="crowdoutneedle", scope_key="wanted-repo", limit=5))
    assert target_id in {item["memory"]["id"] for item in result["items"]}


@pytest.mark.v21
def test_expired_ttl_filter_is_applied_before_fts_candidate_cutoff(
    database: Database, service: MemoryService
) -> None:
    expired_at = datetime.now(UTC) - timedelta(days=1)
    with database.session() as session:
        target = MemoryRow(
            scope_type=ScopeType.REPOSITORY,
            scope_key="ttl-crowdout",
            memory_type=MemoryType.PROJECT,
            category="decision",
            key="ttl.current",
            title="Current record",
            content=" ".join(["filler"] * 200 + ["ttlcrowdoutneedle"]),
            status=MemoryStatus.ACTIVE,
            confidence=0.9,
            importance=0.9,
            created_by=CreatedBy.IMPORT,
            sensitivity=Sensitivity.NORMAL,
            metadata_json={},
        )
        session.add(target)
        session.flush()
        target_id = target.id
        session.add_all(
            [
                MemoryRow(
                    scope_type=ScopeType.REPOSITORY,
                    scope_key="ttl-crowdout",
                    memory_type=MemoryType.PROJECT,
                    category="ttlcrowdoutneedle",
                    subject="ttlcrowdoutneedle",
                    key=f"ttl.expired.{index}",
                    title=f"ttlcrowdoutneedle {index}",
                    content="ttlcrowdoutneedle",
                    status=MemoryStatus.ACTIVE,
                    confidence=0.9,
                    importance=0.9,
                    ttl_seconds=1,
                    created_at=expired_at,
                    updated_at=expired_at,
                    created_by=CreatedBy.IMPORT,
                    sensitivity=Sensitivity.NORMAL,
                    metadata_json={},
                )
                for index in range(1000)
            ]
        )
    with database.session() as session:
        ranked_ids = [
            str(row.memory_id)
            for row in session.execute(
                text(
                    "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH "
                    "'ttlcrowdoutneedle' ORDER BY "
                    "bm25(memory_fts, 0.0, 5.0, 2.5, 1.0, 2.0, 1.0)"
                )
            )
        ]
    assert ranked_ids.index(target_id) >= 640

    result = service.search(
        SearchRequest(query="ttlcrowdoutneedle", scope_key="ttl-crowdout", limit=5)
    )

    assert target_id in {item["memory"]["id"] for item in result["items"]}


@pytest.mark.v21
def test_retrieval_offset_is_applied_after_a_large_enough_candidate_window(
    database: Database, service: MemoryService
) -> None:
    with database.session() as session:
        session.add_all(
            [
                MemoryRow(
                    scope_type=ScopeType.REPOSITORY,
                    scope_key="paged-repo",
                    memory_type=MemoryType.PROJECT,
                    category="decision",
                    key=f"pagination.{index}",
                    title=f"paginationneedle record {index}",
                    content=f"paginationneedle unique-{index}",
                    status=MemoryStatus.ACTIVE,
                    confidence=0.9,
                    importance=0.9,
                    created_by=CreatedBy.IMPORT,
                    sensitivity=Sensitivity.NORMAL,
                    metadata_json={},
                )
                for index in range(400)
            ]
        )

    result = service.search(
        SearchRequest(query="paginationneedle", scope_key="paged-repo", limit=10, offset=320)
    )

    assert result["total"] == 400
    assert len(result["items"]) == 10


@pytest.mark.v21
def test_import_rejects_oversized_uncompressed_entry_before_reading_it(
    tmp_path: Path,
    database: Database,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memoryos.backup.service as backup_module

    payload = (
        json.dumps({"type": "setting", "data": {"key": "x", "value": "y" * 200}}) + "\n"
    ).encode()
    manifest = {
        "format": "memoryos-jsonl-export",
        "format_version": 3,
        "data_sha256": hashlib.sha256(payload).hexdigest(),
        "records": 1,
    }
    archive_path = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.jsonl", payload)
    monkeypatch.setattr(backup_module, "MAX_JSONL_IMPORT_BYTES", 64)
    with pytest.raises(BackupError, match="allowed size"):
        BackupService(database, settings).import_jsonl(archive_path)


@pytest.mark.v21
def test_import_rejects_nonfinite_embedding_before_database_mutation(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    memory = service.propose(
        _explicit_memory("nan-vector", scope_key="import-vector"), actor="audit"
    )
    record = {
        "type": "embedding",
        "data": {
            "id": "00000000-0000-0000-0000-000000000001",
            "memory_id": memory["id"],
            "provider": "forged",
            "model": "nan",
            "dimensions": 1,
            "vector_json": [float("nan")],
            "created_at": datetime.now(UTC).isoformat(),
        },
    }
    payload = (json.dumps(record) + "\n").encode()
    manifest = {
        "format": "memoryos-jsonl-export",
        "format_version": 3,
        "data_sha256": hashlib.sha256(payload).hexdigest(),
        "records": 1,
    }
    archive_path = tmp_path / "nan-vector.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data.jsonl", payload)

    with pytest.raises(BackupError, match="non-finite"):
        BackupService(database, settings).import_jsonl(archive_path)
    with database.session() as session:
        assert int(session.scalar(select(func.count()).select_from(EmbeddingRow)) or 0) == 0


@pytest.mark.v21
def test_restore_rejects_database_that_claims_head_but_is_missing_schema(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    original = service.propose(
        _explicit_memory("survives-invalid-restore", scope_key="restore-schema"),
        actor="audit",
    )
    forged_database = tmp_path / "forged.db"
    connection = sqlite3.connect(forged_database)
    try:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0003_reality_intelligence_hardening');
            CREATE TABLE memories (id VARCHAR(36) NOT NULL PRIMARY KEY);
            """
        )
        connection.commit()
    finally:
        connection.close()
    database_bytes = forged_database.read_bytes()
    manifest = {
        "format": "memoryos-sqlite-backup",
        "format_version": 3,
        "schema_version": "0003_reality_intelligence_hardening",
        "database_sha256": hashlib.sha256(database_bytes).hexdigest(),
    }
    archive_path = tmp_path / "forged.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memoryos.db", database_bytes)

    with pytest.raises(BackupError, match="schema"):
        BackupService(database, settings).restore(archive_path, create_safety_backup=False)
    assert service.get(original["id"])["status"] == "active"


@pytest.mark.v21
def test_restore_rejects_database_missing_a_required_index(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    original = service.propose(
        _explicit_memory("survives-index-forgery", scope_key="restore-index"), actor="audit"
    )
    backups = BackupService(database, settings)
    valid_archive = backups.create_backup(tmp_path / "valid-index-source.zip")
    with zipfile.ZipFile(valid_archive) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        database_bytes = archive.read("memoryos.db")
    forged_database = tmp_path / "missing-index.db"
    forged_database.write_bytes(database_bytes)
    connection = sqlite3.connect(forged_database)
    try:
        connection.execute("DROP INDEX ix_claims_memory_status")
        connection.commit()
    finally:
        connection.close()
    forged_bytes = forged_database.read_bytes()
    manifest["database_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    forged_archive = tmp_path / "missing-index.zip"
    with zipfile.ZipFile(forged_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memoryos.db", forged_bytes)

    with pytest.raises(BackupError, match="schema"):
        backups.restore(forged_archive, create_safety_backup=False)
    assert service.get(original["id"])["status"] == "active"


@pytest.mark.v21
def test_restore_rejects_database_with_an_injected_trigger(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    original = service.propose(
        _explicit_memory("survives-trigger-forgery", scope_key="restore-trigger"),
        actor="audit",
    )
    backups = BackupService(database, settings)
    valid_archive = backups.create_backup(tmp_path / "valid-trigger-source.zip")
    with zipfile.ZipFile(valid_archive) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        database_bytes = archive.read("memoryos.db")
    forged_database = tmp_path / "injected-trigger.db"
    forged_database.write_bytes(database_bytes)
    connection = sqlite3.connect(forged_database)
    try:
        connection.execute(
            "CREATE TRIGGER injected_memory_wipe AFTER INSERT ON memories "
            "BEGIN DELETE FROM memories WHERE id != NEW.id; END"
        )
        connection.commit()
    finally:
        connection.close()
    forged_bytes = forged_database.read_bytes()
    manifest["database_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    forged_archive = tmp_path / "injected-trigger.zip"
    with zipfile.ZipFile(forged_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memoryos.db", forged_bytes)

    with pytest.raises(BackupError, match="schema"):
        backups.restore(forged_archive, create_safety_backup=False)
    assert service.get(original["id"])["status"] == "active"


@pytest.mark.v21
def test_restore_rejects_semantically_invalid_rows(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    original = service.propose(
        _explicit_memory("survives-row-forgery", scope_key="restore-row-data"),
        actor="audit",
    )
    backups = BackupService(database, settings)
    valid_archive = backups.create_backup(tmp_path / "valid-row-source.zip")
    with zipfile.ZipFile(valid_archive) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        database_bytes = archive.read("memoryos.db")
    forged_database = tmp_path / "invalid-row.db"
    forged_database.write_bytes(database_bytes)
    connection = sqlite3.connect(forged_database)
    try:
        connection.execute(
            "UPDATE memories SET confidence = 5.0, metadata_json = '[]' WHERE id = ?",
            (original["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    forged_bytes = forged_database.read_bytes()
    manifest["database_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    forged_archive = tmp_path / "invalid-row.zip"
    with zipfile.ZipFile(forged_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memoryos.db", forged_bytes)

    with pytest.raises(BackupError, match="backup database"):
        backups.restore(forged_archive, create_safety_backup=False)
    assert service.get(original["id"])["status"] == "active"


@pytest.mark.v21
def test_restore_rolls_back_live_database_when_activation_fails(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.propose(_explicit_memory("backup-state", scope_key="restore-rollback"), actor="audit")
    backups = BackupService(database, settings)
    archive_path = backups.create_backup(tmp_path / "rollback-source.zip")
    live_only = service.propose(
        _explicit_memory(
            "live-only", scope_key="restore-rollback", subject="project.live_component"
        ),
        actor="audit",
    )
    initialize = database.initialize
    calls = 0

    def fail_first_activation() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected activation failure")
        initialize()

    monkeypatch.setattr(database, "initialize", fail_first_activation)
    with pytest.raises(BackupError, match="rolled back"):
        backups.restore(archive_path, create_safety_backup=False)
    assert calls == 2
    assert service.get(live_only["id"])["status"] == "active"


@pytest.mark.v21
def test_restore_discards_external_ann_cache_and_persisted_state(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    settings: Any,
) -> None:
    memory = service.propose(
        _explicit_memory("ann-restore", scope_key="restore-ann"), actor="audit"
    )
    engine = RetrievalEngine(database, _FixtureEmbedding())
    try:
        assert engine.index_memory(memory["id"])
    finally:
        engine.close()
    archive_path = BackupService(database, settings).create_backup(tmp_path / "ann-source.zip")
    assert list(settings.ann_dir.glob("*.sqlite"))

    BackupService(database, settings).restore(archive_path, create_safety_backup=False)

    assert not list(settings.ann_dir.glob("*.sqlite"))
    with database.session() as session:
        assert int(session.scalar(select(func.count()).select_from(EmbeddingRow)) or 0) == 1
        assert int(session.scalar(select(func.count()).select_from(AnnIndexStateRow)) or 0) == 0
