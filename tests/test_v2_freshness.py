from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

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

    moved_source.unlink()
    _commit(repository, "delete obsolete auth implementation")
    stale = anchors.refresh(memory_id=memory["id"], repository_path=repository)
    assert stale["freshness"] == "stale"
