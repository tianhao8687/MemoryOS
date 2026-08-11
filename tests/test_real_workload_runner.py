from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import RetrievalRunRow
from memoryos.evaluation.real_workload_agent import (
    AgentEvidenceType,
    AgentExecutionEvidence,
    AgentOutput,
    AgentRuntimeSpec,
)
from memoryos.evaluation.real_workload_containers import ContainerCommandResult
from memoryos.evaluation.real_workload_models import ExperimentCondition, RealWorkloadManifest
from memoryos.evaluation.real_workload_report import RunMode
from memoryos.evaluation.real_workload_runner import RealWorkloadRunner
from memoryos.evaluation.real_workload_scoring import HiddenTestResult

IMAGE = "fixture@sha256:" + "a" * 64
MCP_IMAGE = "fixture-mcp@sha256:" + "b" * 64
HIDDEN_IMAGE = "python@sha256:" + "c" * 64


def _git(root: Path, *arguments: str, at: str | None = None) -> str:
    executable = shutil.which("git")
    assert executable is not None
    environment = os.environ.copy()
    if at:
        environment.update({"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at})
    result = subprocess.run(  # noqa: S603 - fixed test-only git inputs
        [executable, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


class FixtureAgentExecutor:
    def run(
        self,
        spec: AgentRuntimeSpec,
        workspace: object,
        memory: object,
        prompt_path: Path,
        output_dir: Path,
    ) -> AgentExecutionEvidence:
        del spec
        output_dir.mkdir(parents=True)
        stdout = output_dir / "stdout.log"
        stderr = output_dir / "stderr.log"
        stdout.write_text("fixture agent\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        workspace.path.joinpath("app.py").write_text(  # type: ignore[attr-defined]
            "VALUE = 2\n", encoding="utf-8"
        )
        if memory.audit_path is not None:  # type: ignore[attr-defined]
            memory.audit_path.write_text(  # type: ignore[attr-defined]
                '{"backend":"'
                + memory.condition.value  # type: ignore[attr-defined]
                + '","ok":true,"selected_seed_ids":["decision"]}\n',
                encoding="utf-8",
            )
        if memory.data_dir is not None:  # type: ignore[attr-defined]
            database = Database(settings_for(memory.data_dir))  # type: ignore[attr-defined]
            generated = memory.generated_memory_ids  # type: ignore[attr-defined]
            with database.session() as session:
                session.add(
                    RetrievalRunRow(
                        query="fixture",
                        task="fixture",
                        scope_json={},
                        selected_memory_ids=[generated["decision"]],
                        candidate_features=[],
                        context_manifest=[],
                        config_hash="d" * 64,
                    )
                )
            database.close()
        container = ContainerCommandResult(
            exit_code=0,
            duration_seconds=0.1,
            stdout_path=stdout,
            stderr_path=stderr,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
        )
        return AgentExecutionEvidence(
            provider="fixture",
            model="fixture-model",
            agent_version="1",
            image=IMAGE,
            prompt_sha256=hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
            result=AgentOutput(
                status="completed",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0,
                tool_calls=1,
            ),
            container=container,
        )


class FixtureHiddenRunner:
    def run(
        self,
        workspace: object,
        spec: object,
        *,
        hidden_root: Path,
        output_dir: Path,
        container_user: str | None = None,
    ) -> HiddenTestResult:
        del workspace, hidden_root, container_user
        output_dir.mkdir(parents=True)
        return HiddenTestResult(
            success=True,
            image=spec.image,  # type: ignore[attr-defined]
            command_sha256="e" * 64,
            expected_exit_code=0,
            actual_exit_code=0,
            hidden_patch_sha256=None,
            hidden_patch_applied=False,
            setup_error_code=None,
            container=None,
        )


def test_runner_executes_three_isolated_conditions_and_writes_truthful_report(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "bench@example.invalid")
    _git(source, "config", "user.name", "Fixture")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "base", at="2025-01-01T00:00:00+00:00")
    base = _git(source, "rev-parse", "HEAD")
    other_source = tmp_path / "other-source"
    other_source.mkdir()
    _git(other_source, "init", "-b", "main")
    _git(other_source, "config", "user.email", "bench@example.invalid")
    _git(other_source, "config", "user.name", "Fixture")
    (other_source / "private.txt").write_text("OTHER-PROJECT-CANARY\n", encoding="utf-8")
    _git(other_source, "add", "private.txt")
    _git(other_source, "commit", "-m", "other base", at="2025-01-01T00:00:00+00:00")
    manifest = RealWorkloadManifest.model_validate(
        {
            "name": "runner-fixture",
            "tier": "harness_fixture",
            "generated_at": "2026-08-10T00:00:00Z",
            "repositories": [
                {"id": "project", "clone_url": str(source), "license_spdx": "MIT"},
                {
                    "id": "other-project",
                    "clone_url": str(other_source),
                    "license_spdx": "MIT",
                },
            ],
            "memories": [
                {
                    "id": "decision",
                    "repository_id": "project",
                    "category": "decision",
                    "title": "Use value two",
                    "content": "Set the fixture value to two.",
                    "captured_at": "2025-01-02T00:00:00Z",
                    "source_ref": "fixture decision",
                },
                {
                    "id": "other-canary",
                    "repository_id": "other-project",
                    "category": "privacy",
                    "title": "Other project marker",
                    "content": "Never expose OTHER-PROJECT-CANARY.",
                    "captured_at": "2025-01-02T00:00:00Z",
                    "source_ref": "private.txt",
                    "expectation": "cross_project_guard",
                    "canary": "OTHER-PROJECT-CANARY",
                },
            ],
            "tasks": [
                {
                    "id": "set-value",
                    "repository_id": "project",
                    "sequence_id": "value-sequence",
                    "sequence_index": 1,
                    "base_commit": base,
                    "cutoff": "2025-02-01T00:00:00Z",
                    "prompt": "Set VALUE to 2.",
                    "memory_seed_ids": ["decision", "other-canary"],
                    "hidden_test": {
                        "image": HIDDEN_IMAGE,
                        "command": ["python", "-m", "pytest"],
                    },
                }
            ],
        }
    )
    runtime = AgentRuntimeSpec(
        image=IMAGE,
        mcp_image=MCP_IMAGE,
        command=[
            "fixture",
            "{workspace}",
            "{prompt_file}",
            "{mcp_config}",
            "{result_file}",
        ],
        provider="fixture",
        model="fixture-model",
        agent_version="1",
        evidence_type=AgentEvidenceType.DETERMINISTIC_FIXTURE,
    )
    hidden_root = tmp_path / "hidden"
    hidden_root.mkdir()
    runner = RealWorkloadRunner(
        tmp_path / "work",
        agent_executor=FixtureAgentExecutor(),  # type: ignore[arg-type]
        hidden_runner=FixtureHiddenRunner(),  # type: ignore[arg-type]
    )

    report = runner.run(
        manifest,
        runtime,
        hidden_root=hidden_root,
        output_root=tmp_path / "evidence",
        mode=RunMode.DRY_RUN,
        run_id="fixture-run",
        order_seed=7,
    )

    assert report["status"] == "completed"
    assert report["effect_claim"] == "none"
    assert report["sample_size"] == 1
    assert report["condition_run_count"] == 3
    assert len({record["prompt_sha256"] for record in report["records"]}) == 1
    assert report["aggregates"]["flat_memory"]["memory_tool_calls"] == 1
    assert report["aggregates"]["memoryos"]["retrieval_runs"] == 1
    assert (tmp_path / "evidence" / "fixture-run" / "report.json").exists()
    assert (tmp_path / "evidence" / "fixture-run" / "run-metadata.json").exists()
    assert report["temporal_validation"][0]["checked_task_ids"] == ("set-value",)
    assert report["temporal_validation"][1]["checked_task_ids"] == ()
    assert datetime.fromisoformat(report["finished_at"]).tzinfo is not None

    calibration = runner.run(
        manifest,
        runtime,
        hidden_root=hidden_root,
        output_root=tmp_path / "evidence",
        mode=RunMode.DRY_RUN,
        run_id="fixture-memoryos-calibration",
        conditions=[ExperimentCondition.MEMORYOS],
        order_seed=7,
    )

    assert calibration["status"] == "completed_invalid"
    assert calibration["condition_run_count"] == 1
    assert calibration["records"][0]["condition"] == "memoryos"
    assert "does not have all three conditions" in " ".join(calibration["protocol_errors"])
