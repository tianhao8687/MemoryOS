from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from memoryos.db.models import ClaimEvidenceRow, ClaimRow, SourceAnchorRow
from memoryos.db.session import Database
from memoryos.engine import MemoryService
from memoryos.freshness import SourceAnchorService
from memoryos.freshness.tree_sitter_adapter import locate_symbol, parser_backend


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - controlled test fixture command
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _migrate(database: Database, target: str, *, downgrade: bool = False) -> None:
    migrations = Path(__file__).resolve().parents[1] / "memoryos" / "db" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, target)
        else:
            command.upgrade(config, target)


@pytest.mark.v2
@pytest.mark.parametrize(
    ("language", "source", "symbol", "marker"),
    [
        (
            "python",
            "class First:\n    def run(self):\n        return 1\n\n"
            "class Second:\n    def run(self):\n        return 2\n",
            "Second.run",
            "return 2",
        ),
        (
            "typescript",
            "export class Store {\n  refresh(): number { return 2 }\n}\n",
            "Store.refresh",
            "return 2",
        ),
        (
            "javascript",
            "export const refresh = () => { return 2 }\n",
            "refresh",
            "return 2",
        ),
        ("rust", "pub fn refresh() -> i32 { 2 }\n", "refresh", "{ 2 }"),
    ],
)
def test_a20_tree_sitter_symbol_anchors(
    language: str,
    source: str,
    symbol: str,
    marker: str,
) -> None:
    assert parser_backend(language) == "tree-sitter"
    located = locate_symbol(source, language, symbol)

    assert located is not None
    assert located.backend == "tree-sitter"
    assert located.symbol_fqn == symbol
    assert marker in located.excerpt
    assert located.line_end >= located.line_start


@pytest.mark.v2
def test_a20_a21_a22_git_fresh_moved_suspect_and_stale(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    repository = tmp_path / "freshness-repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "MemoryOS Test")
    _git(repository, "config", "user.email", "memoryos@example.invalid")
    source = repository / "src" / "service.py"
    source.parent.mkdir()
    source.write_text(
        "def authenticate(user: str) -> bool:\n"
        '    """Validate the local session token."""\n'
        "    return bool(user)\n",
        encoding="utf-8",
    )
    _commit(repository, "initial auth symbol")

    memory = service.propose(
        make_memory(
            title="Authentication implementation",
            content="Authentication is implemented in src/service.py at authenticate.",
            key="implementation.authentication",
        ),
        actor="test",
    )
    anchors = SourceAnchorService(database)
    created = anchors.create(
        memory_id=memory["id"],
        repository_path=repository,
        path="src/service.py",
        symbol_fqn="authenticate",
    )
    assert created["freshness_state"] == "fresh"
    assert created["parser_backend"] == "tree-sitter"
    assert (
        anchors.refresh(memory_id=memory["id"], repository_path=repository)["freshness"] == "fresh"
    )

    _git(repository, "mv", "src/service.py", "src/auth.py")
    _commit(repository, "move auth symbol")
    moved = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert moved["freshness"] == "moved"
    assert moved["anchors"][0]["path"] == "src/auth.py"

    moved_source = repository / "src" / "auth.py"
    moved_source.write_text(
        "def authenticate(user: str) -> bool:\n"
        "    raise RuntimeError('authentication was replaced')\n",
        encoding="utf-8",
    )
    _commit(repository, "replace auth implementation")
    changed = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert changed["freshness"] in {"suspect", "stale"}
    assert changed["replacement_candidate"]["status"] == "candidate"

    (repository / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    _commit(repository, "unrelated commit after suspect evidence")
    unchanged = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert unchanged["freshness"] in {"suspect", "stale"}
    assert unchanged["anchors"][0]["original_path"] == "src/service.py"
    assert unchanged["anchors"][0]["original_excerpt_hash"] == created["excerpt_hash"]

    moved_source.unlink()
    _commit(repository, "delete obsolete auth implementation")
    stale = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert stale["freshness"] == "stale"


def _anchored_memory(
    tmp_path: Path,
    service: MemoryService,
    make_memory: Any,
) -> tuple[Path, Path, dict[str, Any], SourceAnchorService, dict[str, Any]]:
    repository = tmp_path / "immutable-anchor-repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "MemoryOS Test")
    _git(repository, "config", "user.email", "memoryos@example.invalid")
    source = repository / "settings.py"
    source.write_text('def backend():\n    return "sqlite"\n', encoding="utf-8")
    _commit(repository, "record original evidence")
    memory = service.propose(
        make_memory(
            title="Backend implementation",
            content="The backend is selected by settings.backend.",
            key="implementation.backend",
        ),
        actor="test",
    )
    anchors = SourceAnchorService(service.database)
    created = anchors.create(
        memory_id=memory["id"],
        repository_path=repository,
        path="settings.py",
        symbol_fqn="backend",
    )
    return repository, source, memory, anchors, created


@pytest.mark.v22
def test_refresh_cannot_launder_changed_evidence_back_to_fresh(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    repository, source, memory, anchors, created = _anchored_memory(tmp_path, service, make_memory)
    original_hash = created["excerpt_hash"]

    source.write_text('def backend():\n    return "postgresql"\n', encoding="utf-8")
    _commit(repository, "replace anchored evidence")
    changed = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert changed["freshness"] in {"suspect", "stale"}

    with database.session() as session:
        anchor = session.scalar(select(SourceAnchorRow))
        assert anchor is not None
        assert anchor.excerpt_hash == original_hash
        assert anchor.path == "settings.py"
        assert anchor.observed_excerpt_hash != original_hash

    (repository / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    _commit(repository, "change an unrelated file")
    refreshed = anchors.refresh(memory_id=memory["id"], repository_path=repository)

    assert refreshed["freshness"] in {"suspect", "stale"}
    assert refreshed["anchors"][0]["original_excerpt_hash"] == original_hash


@pytest.mark.v22
def test_pure_move_remains_observed_without_mutating_anchor_baseline(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    repository, _source, memory, anchors, created = _anchored_memory(tmp_path, service, make_memory)
    _git(repository, "mv", "settings.py", "runtime_settings.py")
    _commit(repository, "move anchored evidence")

    moved = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert moved["freshness"] == "moved"
    assert moved["anchors"][0]["path"] == "runtime_settings.py"

    with database.session() as session:
        anchor = session.scalar(select(SourceAnchorRow))
        assert anchor is not None
        assert anchor.path == "settings.py"
        assert anchor.excerpt_hash == created["excerpt_hash"]
        assert anchor.observed_path == "runtime_settings.py"

    (repository / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    _commit(repository, "unrelated commit after move")
    refreshed = anchors.refresh(memory_id=memory["id"], repository_path=repository)

    assert refreshed["freshness"] == "moved"
    assert refreshed["anchors"][0]["original_path"] == "settings.py"
    assert refreshed["anchors"][0]["observed_path"] == "runtime_settings.py"


@pytest.mark.v22
def test_explicit_reanchor_creates_a_new_baseline_and_preserves_history(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    repository, source, memory, anchors, created = _anchored_memory(tmp_path, service, make_memory)
    source.write_text('def backend():\n    return "postgresql"\n', encoding="utf-8")
    _commit(repository, "replace anchored evidence")
    anchors.refresh(memory_id=memory["id"], repository_path=repository)

    replacement = anchors.create(
        memory_id=memory["id"],
        repository_path=repository,
        path="settings.py",
        symbol_fqn="backend",
    )

    assert replacement["id"] != created["id"]
    assert replacement["excerpt_hash"] != created["excerpt_hash"]
    with database.session() as session:
        rows = list(session.scalars(select(SourceAnchorRow).order_by(SourceAnchorRow.created_at)))
        assert len(rows) == 2
        assert rows[0].excerpt_hash == created["excerpt_hash"]
        assert rows[1].excerpt_hash == replacement["excerpt_hash"]
        claim_id = session.scalar(select(ClaimRow.id).where(ClaimRow.memory_id == memory["id"]))
        assert claim_id is not None
        linked_anchor = session.scalar(
            select(ClaimEvidenceRow.source_anchor_id).where(ClaimEvidenceRow.claim_id == claim_id)
        )
        assert linked_anchor == replacement["id"]


@pytest.mark.v22
def test_anchor_observation_migration_upgrades_and_replays_without_history_loss(
    database: Database,
) -> None:
    _migrate(database, "0003_reality_intelligence_hardening", downgrade=True)
    recorded_at = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO source_anchors (id,repository_stable_key,commit_sha,path,blob_sha,"
            "language,symbol_fqn,symbol_kind,line_start,line_end,evidence_excerpt,excerpt_hash,"
            "context_hash,freshness_state,cached_head,checked_at,metadata_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "anchor-v21",
                "repo-stable-key",
                "a" * 40,
                "src/original.py",
                "b" * 40,
                "python",
                "backend",
                "function",
                3,
                4,
                "def backend():\n    return 'sqlite'",
                "c" * 64,
                "d" * 64,
                "fresh",
                "a" * 40,
                recorded_at,
                "{}",
                recorded_at,
            ),
        )

    _migrate(database, "head")
    with database.session() as session:
        rows = list(session.scalars(select(SourceAnchorRow)))
        assert len(rows) == 1
        assert rows[0].path == "src/original.py"
        assert rows[0].excerpt_hash == "c" * 64
        assert rows[0].observed_path == "src/original.py"
        assert rows[0].observed_excerpt_hash == "c" * 64

    _migrate(database, "0003_reality_intelligence_hardening", downgrade=True)
    _migrate(database, "head")
    with database.session() as session:
        replayed = list(session.scalars(select(SourceAnchorRow)))
        assert len(replayed) == 1
        assert replayed[0].path == "src/original.py"
        assert replayed[0].excerpt_hash == "c" * 64
        assert replayed[0].observed_path == "src/original.py"
