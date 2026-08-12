from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    RepositorySpec,
    WorkloadTaskSpec,
)
from memoryos.evaluation.real_workload_workspace import (
    RepositoryWorkspaceManager,
    WorkspaceError,
)

IMAGE = "python:3.12-slim@sha256:" + "a" * 64


def _git(root: Path, *arguments: str, at: str | None = None, check: bool = True) -> str:
    executable = shutil.which("git")
    assert executable is not None
    environment = os.environ.copy()
    if at:
        environment.update({"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at})
    result = subprocess.run(  # noqa: S603 - fixed test-only git inputs
        [executable, *arguments],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "bench@example.invalid")
    _git(root, "config", "user.name", "Benchmark Fixture")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "base", at="2025-01-01T00:00:00+00:00")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "commit", "-am", "solution", at="2025-03-01T00:00:00+00:00")
    solution = _git(root, "rev-parse", "HEAD")
    return root, base, solution


def _task(base: str, solution: str) -> WorkloadTaskSpec:
    return WorkloadTaskSpec.model_validate(
        {
            "id": "change-value",
            "repository_id": "fixture",
            "sequence_id": "value-history",
            "sequence_index": 1,
            "base_commit": base,
            "solution_commit": solution,
            "cutoff": "2025-02-01T00:00:00Z",
            "prompt": "Change the value without reading future history.",
            "hidden_test": {"image": IMAGE, "command": ["python", "-m", "pytest"]},
        }
    )


def test_materialized_workspace_excludes_solution_and_remotes(
    tmp_path: Path, repository: tuple[Path, str, str]
) -> None:
    source, base, solution = repository
    manager = RepositoryWorkspaceManager(tmp_path / "bench")
    prepared = manager.prepare_repository(
        RepositorySpec(
            id="fixture",
            clone_url=str(source),
            license_spdx="MIT",
        )
    )
    task = _task(base, solution)
    manager.assert_manifest_commits(prepared, [task])

    workspace = manager.materialize(
        prepared,
        task,
        ExperimentCondition.NO_MEMORY,
        run_id="run-001",
    )

    assert _git(workspace.path, "rev-parse", "HEAD") == base
    assert _git(workspace.path, "remote") == ""
    assert _git(workspace.path, "config", "--get", "core.longpaths") == "true"
    assert not (workspace.path / ".git" / "FETCH_HEAD").exists()
    _git(workspace.path, "cat-file", "-e", f"{solution}^{{commit}}", check=False)
    executable = shutil.which("git")
    assert executable is not None
    missing = subprocess.run(  # noqa: S603 - fixed test-only git inputs
        [executable, "cat-file", "-e", f"{solution}^{{commit}}"],
        cwd=workspace.path,
        check=False,
    )
    assert missing.returncode != 0


def test_patch_capture_includes_tracked_and_untracked_files(
    tmp_path: Path, repository: tuple[Path, str, str]
) -> None:
    source, base, solution = repository
    manager = RepositoryWorkspaceManager(tmp_path / "bench")
    prepared = manager.prepare_repository(
        RepositorySpec(id="fixture", clone_url=str(source), license_spdx="MIT")
    )
    task = _task(base, solution)
    agent = manager.materialize(
        prepared,
        task,
        ExperimentCondition.MEMORYOS,
        run_id="run-002",
    )
    (agent.path / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    (agent.path / "new.py").write_text("NEW = True\n", encoding="utf-8")

    patch = manager.capture_patch(agent, agent.path.parent / "agent.patch")

    assert patch.size_bytes > 0
    assert patch.changed_files == ("app.py", "new.py")
    scorer = manager.materialize(
        prepared,
        task,
        ExperimentCondition.NO_MEMORY,
        run_id="run-002-score",
    )
    manager.apply_captured_patch(scorer, patch)
    assert (scorer.path / "app.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert (scorer.path / "new.py").read_text(encoding="utf-8") == "NEW = True\n"


def test_patch_capture_includes_changes_committed_by_agent(
    tmp_path: Path, repository: tuple[Path, str, str]
) -> None:
    source, base, solution = repository
    manager = RepositoryWorkspaceManager(tmp_path / "bench")
    prepared = manager.prepare_repository(
        RepositorySpec(id="fixture", clone_url=str(source), license_spdx="MIT")
    )
    task = _task(base, solution)
    agent = manager.materialize(
        prepared,
        task,
        ExperimentCondition.MEMORYOS,
        run_id="run-committed",
    )
    (agent.path / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    _git(agent.path, "add", "app.py")
    _git(
        agent.path,
        "-c",
        "user.email=agent@example.invalid",
        "-c",
        "user.name=Agent Fixture",
        "commit",
        "-m",
        "agent change",
    )

    patch = manager.capture_patch(agent, agent.path.parent / "committed.patch")

    assert patch.changed_files == ("app.py",)
    assert patch.size_bytes > 0
    scorer = manager.materialize(
        prepared,
        task,
        ExperimentCondition.NO_MEMORY,
        run_id="run-committed-score",
    )
    manager.apply_captured_patch(scorer, patch)
    assert (scorer.path / "app.py").read_text(encoding="utf-8") == "VALUE = 4\n"


def test_patch_capture_rejects_agent_modified_git_config_before_running_git(
    tmp_path: Path, repository: tuple[Path, str, str]
) -> None:
    source, base, solution = repository
    manager = RepositoryWorkspaceManager(tmp_path / "bench")
    prepared = manager.prepare_repository(
        RepositorySpec(id="fixture", clone_url=str(source), license_spdx="MIT")
    )
    task = _task(base, solution)
    agent = manager.materialize(
        prepared,
        task,
        ExperimentCondition.FLAT_MEMORY,
        run_id="run-config-tamper",
    )
    (agent.path / ".git" / "config").write_text(
        '[filter "host-command"]\n\tclean = calc.exe\n', encoding="utf-8"
    )

    with pytest.raises(WorkspaceError, match="Git control plane"):
        manager.capture_patch(agent, agent.path.parent / "tampered.patch")


def test_workspace_refuses_reuse(tmp_path: Path, repository: tuple[Path, str, str]) -> None:
    source, base, solution = repository
    manager = RepositoryWorkspaceManager(tmp_path / "bench")
    prepared = manager.prepare_repository(
        RepositorySpec(id="fixture", clone_url=str(source), license_spdx="MIT")
    )
    task = _task(base, solution)
    manager.materialize(
        prepared,
        task,
        ExperimentCondition.FLAT_MEMORY,
        run_id="run-003",
    )

    with pytest.raises(WorkspaceError, match="refusing to reuse"):
        manager.materialize(
            prepared,
            task,
            ExperimentCondition.FLAT_MEMORY,
            run_id="run-003",
        )


def test_existing_cache_can_be_reused_without_remote_refresh(
    tmp_path: Path, repository: tuple[Path, str, str]
) -> None:
    source, base, solution = repository
    root = tmp_path / "bench"
    repository_spec = RepositorySpec(id="fixture", clone_url=str(source), license_spdx="MIT")
    online = RepositoryWorkspaceManager(root)
    online.prepare_repository(repository_spec)
    source.rename(tmp_path / "source-offline")

    offline = RepositoryWorkspaceManager(root, refresh_existing_cache=False)
    prepared = offline.prepare_repository(repository_spec)
    offline.assert_manifest_commits(prepared, [_task(base, solution)])


def test_cache_reuse_without_fetch_requires_existing_cache(tmp_path: Path) -> None:
    manager = RepositoryWorkspaceManager(tmp_path / "bench", refresh_existing_cache=False)
    repository_spec = RepositorySpec(
        id="missing",
        clone_url=str(tmp_path / "missing-source"),
        license_spdx="MIT",
    )

    with pytest.raises(WorkspaceError, match="cache is required"):
        manager.prepare_repository(repository_spec)
