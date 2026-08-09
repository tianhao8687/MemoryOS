from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memoryos.db.models import MemoryRow, RelationRow, RepositoryRow
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ConflictStrategy,
    ContextRequest,
    CreatedBy,
    MemoryType,
    ScopeType,
    SearchRequest,
)
from memoryos.engine import MemoryService
from memoryos.errors import ConflictDetectedError, InvalidTransitionError


def test_database_migration_and_sqlite_pragmas(database: Database) -> None:
    database.initialize()
    with database.engine.connect() as connection:
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        fts_table = connection.execute(
            text("SELECT name FROM sqlite_master WHERE name='memory_fts'")
        ).scalar_one()
    assert database.schema_version() == "0001_initial"
    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert fts_table == "memory_fts"
    assert database.integrity_check() == "ok"


def test_database_foreign_key_and_unique_constraints(database: Database) -> None:
    with pytest.raises(IntegrityError), database.session() as session:
        session.add_all(
            [
                RepositoryRow(stable_key="duplicate", name="one", path="C:/one"),
                RepositoryRow(stable_key="duplicate", name="two", path="C:/two"),
            ]
        )
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RelationRow(
                from_memory_id="11111111-1111-1111-1111-111111111111",
                to_memory_id="22222222-2222-2222-2222-222222222222",
                relation_type="supersedes",
            )
        )


def test_candidate_lifecycle_and_illegal_transition(
    service: MemoryService, make_memory: Any
) -> None:
    candidate = service.propose(
        make_memory(created_by=CreatedBy.AGENT, activate_immediately=False), actor="test"
    )
    assert candidate["status"] == "candidate"
    confirmed = service.confirm(candidate["id"], actor="test")
    assert confirmed["status"] == "active"
    with pytest.raises(InvalidTransitionError):
        service.confirm(candidate["id"], actor="test")
    forgotten = service.forget(candidate["id"], actor="test")
    assert forgotten["status"] == "forgotten"
    assert service.search(SearchRequest(query="FastAPI"))["total"] == 0
    history = service.history(memory_id=candidate["id"])
    assert history[0]["status"] == "forgotten"


def test_conflict_requires_resolution_and_preserves_supersession_chain(
    service: MemoryService, make_memory: Any
) -> None:
    current = service.propose(make_memory(title="Backend framework: FastAPI"), actor="test")
    candidate = service.propose(
        make_memory(
            title="Backend framework: Django",
            content="Use Django instead of FastAPI.",
            created_by=CreatedBy.AGENT,
            activate_immediately=False,
            source_ref="agent:proposal",
        ),
        actor="test",
    )
    with pytest.raises(ConflictDetectedError) as captured:
        service.confirm(candidate["id"], actor="test")
    assert current["id"] in captured.value.details["conflict_ids"]
    assert service.conflicts()[0]["candidate"]["id"] == candidate["id"]

    replacement = service.confirm(
        candidate["id"],
        strategy=ConflictStrategy.SUPERSEDE,
        rationale="New approved architecture",
        actor="test",
    )
    assert replacement["status"] == "active"
    assert replacement["supersedes_id"] == current["id"]
    assert service.get(current["id"])["status"] == "superseded"
    timeline = service.history(memory_id=replacement["id"])
    assert [item["status"] for item in timeline] == ["superseded", "active"]
    explanation = service.explain(replacement["id"])
    assert explanation["relations"][0]["relation_type"] == "supersedes"
    historical = service.context(
        ContextRequest(
            task="backend framework FastAPI Django",
            repository="repo-a",
            include_historical=True,
        )
    )
    assert current["id"] in {
        item["id"] for item in historical["sections"]["HISTORICAL / SUPERSEDED"]
    }


def test_manual_immediate_activation_cannot_bypass_conflict(
    service: MemoryService, make_memory: Any
) -> None:
    current = service.propose(make_memory(), actor="test")
    with pytest.raises(ConflictDetectedError):
        service.propose(
            make_memory(
                title="Use Django",
                content="Use Django instead of FastAPI.",
                source_ref="manual:conflicting-immediate-write",
            ),
            actor="test",
        )
    assert service.get(current["id"])["status"] == "active"
    assert service.search(SearchRequest(query="Django"))["total"] == 0


def test_ttl_expiration_is_excluded_but_auditable(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    memory = service.propose(
        make_memory(
            title="Temporary auth branch state",
            content="Working on login session rotation.",
            memory_type=MemoryType.WORKING,
            category="state",
            ttl_seconds=1,
        ),
        actor="test",
    )
    with database.session() as session:
        row = session.get(MemoryRow, memory["id"])
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(seconds=5)
    assert service.search(SearchRequest(query="session rotation"))["total"] == 0
    assert service.get(memory["id"])["status"] == "expired"
    assert any(event["action"] == "expire" for event in service.explain(memory["id"])["audit"])


def test_task_scope_receives_default_ttl(service: MemoryService, make_memory: Any) -> None:
    memory = service.propose(
        make_memory(
            title="Current task state",
            content="The current task is to finish the login flow.",
            scope_type=ScopeType.TASK,
            scope_key="task-login",
            memory_type=MemoryType.WORKING,
            category="state",
            key="task.current",
        ),
        actor="test",
    )
    assert memory["ttl_seconds"] == 604_800


def test_branch_scope_isolation_in_context(service: MemoryService, make_memory: Any) -> None:
    service.propose(
        make_memory(
            title="Feature auth temporary state",
            content="Login uses a feature-only session experiment.",
            scope_type=ScopeType.BRANCH,
            scope_key="repo-a:feature/auth",
            memory_type=MemoryType.WORKING,
            category="state",
            key="auth.session.experiment",
        ),
        actor="test",
    )
    feature_context = service.context(
        ContextRequest(task="login session", repository="repo-a", branch="feature/auth")
    )
    main_context = service.context(
        ContextRequest(task="login session", repository="repo-a", branch="main")
    )
    assert "feature-only" in feature_context["text"]
    assert "feature-only" not in main_context["text"]


def test_provenance_fts_and_secret_redaction(service: MemoryService, make_memory: Any) -> None:
    memory = service.propose(
        make_memory(
            title="Do not log credentials",
            content="Never log password=supersecret123 or sk-abcdefghijklmnopqrstuv.",
            category="constraint",
            key="security.logging.secrets",
        ),
        actor="test",
    )
    assert "supersecret123" not in memory["content"]
    assert "sk-abcdefghijklmnopqrstuv" not in memory["content"]
    result = service.search(SearchRequest(query="credentials logging"))
    assert result["total"] == 1
    explanation = service.explain(memory["id"])
    assert len(explanation["sources"]) == 1
    assert len(explanation["sources"][0]["content_hash"]) == 64
    assert explanation["audit"][0]["action"] == "create_active"


def test_concurrent_writes_are_transactional(service: MemoryService, make_memory: Any) -> None:
    def write(index: int) -> str:
        memory = service.propose(
            make_memory(
                title=f"Concurrent memory {index}",
                content=f"Concurrent WAL write number {index} completed transactionally.",
                key=f"concurrency.write.{index}",
                source_ref=f"concurrency:{index}",
            ),
            actor=f"worker-{index}",
        )
        return str(memory["id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        memory_ids = list(pool.map(write, range(16)))
    assert len(set(memory_ids)) == 16
    result = service.search(SearchRequest(query="Concurrent WAL write", limit=50))
    assert result["total"] == 16
