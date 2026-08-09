from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from memoryos.db.models import MemoryRow
from memoryos.db.session import Database
from memoryos.integrations.git import discover_git_context, upsert_repository


def _run(path: Path, *args: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run(  # noqa: S603 - test-only executable and arguments
        [executable, "-C", str(path), *args], check=True, capture_output=True
    )


def _repository(path: Path, remote: str) -> None:
    path.mkdir()
    _run(path, "init", "-b", "main")
    _run(path, "config", "user.email", "memoryos@example.invalid")
    _run(path, "config", "user.name", "MemoryOS Test")
    (path / "README.md").write_text("test", encoding="utf-8")
    _run(path, "add", "README.md")
    _run(path, "commit", "-m", "initial")
    _run(path, "remote", "add", "origin", remote)


def test_repository_identity_survives_path_move_and_does_not_hoard_source(
    tmp_path: Path, database: Database
) -> None:
    remote = "https://example.invalid/team/memoryos.git"
    first = tmp_path / "first-location"
    second = tmp_path / "moved-location"
    _repository(first, remote)
    _repository(second, remote)
    first_context = discover_git_context(first)
    second_context = discover_git_context(second)
    assert first_context.stable_key == second_context.stable_key
    assert first_context.branch == "main"
    assert first_context.branch_scope_key == f"{first_context.stable_key}:main"
    first_row = upsert_repository(database, first_context)
    second_row = upsert_repository(database, second_context)
    assert first_row["id"] == second_row["id"]
    assert second_row["path"] == str(second.resolve())
    assert second_row["branch_scope_key"] == f"{second_context.stable_key}:main"
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(MemoryRow)) == 0
