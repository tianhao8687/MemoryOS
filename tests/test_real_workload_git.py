from __future__ import annotations

import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from memoryos.evaluation.real_workload_git import GitHistoryInspector, GitValidationError
from memoryos.evaluation.real_workload_models import RealWorkloadManifest

IMAGE = "python:3.12-slim@sha256:" + "a" * 64


def _git(root: Path, *arguments: str, at: str | None = None) -> str:
    executable = shutil.which("git")
    assert executable is not None
    environment = os.environ.copy()
    if at:
        environment.update({"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at})
    result = subprocess.run(  # noqa: S603 - test helper invokes local git with fixed inputs
        [executable, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _commit(root: Path, filename: str, content: str, message: str, at: str) -> str:
    (root / filename).write_text(content, encoding="utf-8")
    _git(root, "add", filename)
    _git(root, "commit", "-m", message, at=at)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "bench@example.invalid")
    _git(root, "config", "user.name", "Benchmark Fixture")
    source = _commit(
        root,
        "decision.txt",
        "Use explicit Result objects.\n",
        "record decision",
        "2025-01-01T00:00:00+00:00",
    )
    base = _commit(
        root,
        "parser.py",
        "def parse(value):\n    return value\n",
        "parser baseline",
        "2025-01-15T00:00:00+00:00",
    )
    solution = _commit(
        root,
        "parser.py",
        "def parse(value):\n    return {'ok': value}\n",
        "fix parser",
        "2025-03-01T00:00:00+00:00",
    )
    return root, {"source": source, "base": base, "solution": solution}


def _payload(commits: dict[str, str]) -> dict[str, Any]:
    return {
        "name": "history-smoke",
        "tier": "public_replay",
        "generated_at": "2026-08-10T00:00:00Z",
        "repositories": [
            {
                "id": "project",
                "clone_url": "https://github.com/example/project.git",
                "source_url": "https://github.com/example/project",
                "license_spdx": "MIT",
                "license_url": "https://github.com/example/project/blob/main/LICENSE",
            }
        ],
        "memories": [
            {
                "id": "decision",
                "repository_id": "project",
                "category": "architecture",
                "title": "Result objects",
                "content": "Use explicit Result objects.",
                "captured_at": "2025-01-02T00:00:00Z",
                "source_commit": commits["source"],
                "source_ref": "decision.txt",
            }
        ],
        "tasks": [
            {
                "id": "parser-fix",
                "repository_id": "project",
                "sequence_id": "parser",
                "sequence_index": 1,
                "base_commit": commits["base"],
                "solution_commit": commits["solution"],
                "cutoff": "2025-02-01T00:00:00Z",
                "source_url": "https://github.com/example/project/issues/1",
                "source_published_at": "2025-01-05T00:00:00Z",
                "prompt": "Fix parser result handling.",
                "memory_seed_ids": ["decision"],
                "hidden_test": {"image": IMAGE, "command": ["python", "-m", "pytest"]},
            }
        ],
    }


def test_temporal_history_validation_accepts_replay(
    history: tuple[Path, dict[str, str]],
) -> None:
    root, commits = history
    manifest = RealWorkloadManifest.model_validate(_payload(commits))

    report = GitHistoryInspector().validate_repository(root, manifest, "project")

    assert report.checked_task_ids == ("parser-fix",)
    assert set(report.checked_commits) == set(commits.values())
    assert report.manifest_digest == manifest.digest()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"cutoff": "2025-01-10T00:00:00Z"}, "base commit is later"),
        ({"cutoff": "2025-04-01T00:00:00Z"}, "strictly later"),
    ],
)
def test_temporal_history_validation_rejects_invalid_cutoffs(
    history: tuple[Path, dict[str, str]], change: dict[str, str], message: str
) -> None:
    root, commits = history
    payload = _payload(commits)
    payload["tasks"][0].update(change)
    manifest = RealWorkloadManifest.model_validate(payload)

    with pytest.raises(GitValidationError, match=message):
        GitHistoryInspector().validate_repository(root, manifest, "project")


def test_temporal_history_validation_rejects_non_ancestor_source(
    history: tuple[Path, dict[str, str]],
) -> None:
    root, commits = history
    _git(root, "switch", "--orphan", "unrelated")
    unrelated = _commit(
        root,
        "other.txt",
        "unrelated\n",
        "unrelated source",
        "2025-01-01T00:00:00+00:00",
    )
    payload = deepcopy(_payload(commits))
    payload["memories"][0]["source_commit"] = unrelated
    manifest = RealWorkloadManifest.model_validate(payload)

    with pytest.raises(GitValidationError, match="not an ancestor"):
        GitHistoryInspector().validate_repository(root, manifest, "project")


def test_cross_project_memory_only_repository_is_validated(
    tmp_path: Path, history: tuple[Path, dict[str, str]]
) -> None:
    _, commits = history
    other = tmp_path / "other-repo"
    other.mkdir()
    _git(other, "init", "-b", "main")
    _git(other, "config", "user.email", "bench@example.invalid")
    _git(other, "config", "user.name", "Benchmark Fixture")
    other_source = _commit(
        other,
        "private.txt",
        "Do not disclose CROSS-PROJECT-CANARY.\n",
        "record other project decision",
        "2025-01-01T00:00:00+00:00",
    )
    payload = _payload(commits)
    payload["repositories"].append(
        {
            "id": "other-project",
            "clone_url": "https://github.com/example/other-project.git",
            "source_url": "https://github.com/example/other-project",
            "license_spdx": "MIT",
            "license_url": "https://github.com/example/other-project/blob/main/LICENSE",
        }
    )
    payload["memories"].append(
        {
            "id": "other-canary",
            "repository_id": "other-project",
            "category": "privacy",
            "title": "Other project secret",
            "content": "Do not disclose CROSS-PROJECT-CANARY.",
            "captured_at": "2025-01-02T00:00:00Z",
            "source_commit": other_source,
            "source_ref": "private.txt",
            "expectation": "cross_project_guard",
            "canary": "CROSS-PROJECT-CANARY",
        }
    )
    payload["tasks"][0]["memory_seed_ids"].append("other-canary")
    manifest = RealWorkloadManifest.model_validate(payload)

    report = GitHistoryInspector().validate_memory_repository(other, manifest, "other-project")

    assert report.checked_task_ids == ()
    assert report.checked_commits == (other_source,)
