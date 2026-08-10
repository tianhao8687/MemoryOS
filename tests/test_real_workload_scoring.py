from __future__ import annotations

import hashlib
from pathlib import Path

from memoryos.evaluation.real_workload_containers import ContainerCommandResult
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    HiddenTestSpec,
    MemorySeedSpec,
)
from memoryos.evaluation.real_workload_scoring import HiddenTestRunner, scan_canary_leakage
from memoryos.evaluation.real_workload_workspace import MaterializedWorkspace

IMAGE = "python@sha256:" + "a" * 64


class FakeWorkspaceManager:
    def __init__(self) -> None:
        self.applied: list[object] = []

    def apply_captured_patch(self, workspace: object, patch: object) -> None:
        self.applied.append((workspace, patch))


class FakeScoringEngine:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.arguments: list[str] = []
        self.removed: list[str] = []

    def remove_container(self, name: str) -> None:
        self.removed.append(name)

    def run_attached(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        max_log_bytes: int,
    ) -> ContainerCommandResult:
        del timeout_seconds, max_log_bytes
        self.arguments = arguments
        stdout_path.write_text("tests ran\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ContainerCommandResult(
            exit_code=self.exit_code,
            duration_seconds=0.5,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
        )


def _workspace(tmp_path: Path) -> MaterializedWorkspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return MaterializedWorkspace(
        repository_id="project",
        task_id="task",
        condition=ExperimentCondition.MEMORYOS,
        path=root,
        base_commit="1" * 40,
        git_control_sha256="f" * 64,
    )


def test_hidden_tests_apply_verified_overlay_and_run_without_network(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    patch_path = hidden / "tests.patch"
    patch_path.write_text("fixture patch\n", encoding="utf-8")
    digest = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    spec = HiddenTestSpec(
        image=IMAGE,
        command=["python", "-m", "pytest", "-q"],
        hidden_patch="tests.patch",
        hidden_patch_sha256=digest,
    )
    manager = FakeWorkspaceManager()
    engine = FakeScoringEngine()

    result = HiddenTestRunner(manager, engine).run(
        _workspace(tmp_path),
        spec,
        hidden_root=hidden,
        output_dir=tmp_path / "score",
    )

    assert result.success is True
    assert result.hidden_patch_applied is True
    assert len(manager.applied) == 1
    assert "--network" in engine.arguments
    assert engine.arguments[engine.arguments.index("--network") + 1] == "none"
    assert "--read-only" in engine.arguments
    assert "no-new-privileges" in engine.arguments
    assert "--cap-drop" in engine.arguments
    assert engine.arguments[-4:] == ["python", "-m", "pytest", "-q"]
    assert len(engine.removed) == 1


def test_hidden_patch_digest_mismatch_is_scored_as_setup_failure(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "tests.patch").write_text("changed\n", encoding="utf-8")
    spec = HiddenTestSpec(
        image=IMAGE,
        command=["python", "-m", "pytest"],
        hidden_patch="tests.patch",
        hidden_patch_sha256="f" * 64,
    )
    engine = FakeScoringEngine()

    result = HiddenTestRunner(FakeWorkspaceManager(), engine).run(
        _workspace(tmp_path),
        spec,
        hidden_root=hidden,
        output_dir=tmp_path / "score-failed",
    )

    assert result.success is False
    assert result.setup_error_code == "hidden_patch_apply_failed"
    assert engine.arguments == []


def test_canary_scanner_reports_cross_project_and_stale_surfaces(tmp_path: Path) -> None:
    cross = MemorySeedSpec.model_validate(
        {
            "id": "cross",
            "repository_id": "other",
            "category": "decision",
            "title": "Other project",
            "content": "Never expose CROSS-LEAK-CANARY outside the other project.",
            "captured_at": "2025-01-01T00:00:00Z",
            "source_ref": "other.md",
            "expectation": "cross_project_guard",
            "canary": "CROSS-LEAK-CANARY",
        }
    )
    stale = MemorySeedSpec.model_validate(
        {
            "id": "stale",
            "repository_id": "project",
            "category": "decision",
            "title": "Old choice",
            "content": "Do not reuse STALE-USE-CANARY after expiry.",
            "captured_at": "2025-01-01T00:00:00Z",
            "valid_to": "2025-01-02T00:00:00Z",
            "source_ref": "old.md",
            "expectation": "stale",
            "canary": "STALE-USE-CANARY",
        }
    )
    patch = tmp_path / "agent.patch"
    patch.write_text("+ value = 'CROSS-LEAK-CANARY'\n", encoding="utf-8")
    log = tmp_path / "agent.log"
    log.write_text("considered stale-use-canary\n", encoding="utf-8")

    report = scan_canary_leakage(
        [cross, stale],
        patch_path=patch,
        file_surfaces={"agent_log": log},
    )

    assert report.cross_project_leaks == 1
    assert report.stale_memory_uses == 1
    assert {finding.surface for finding in report.findings} == {"agent_patch", "agent_log"}
    assert "CROSS-LEAK-CANARY" not in str(report.as_dict())
