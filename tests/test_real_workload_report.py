from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memoryos.evaluation.real_workload_agent import AgentEvidenceType, AgentRuntimeSpec
from memoryos.evaluation.real_workload_models import ExperimentCondition, RealWorkloadManifest
from memoryos.evaluation.real_workload_report import (
    ConditionRunRecord,
    RealWorkloadReportBuilder,
    RunMode,
)

HIDDEN_IMAGE = "python@sha256:" + "c" * 64


def _runtime() -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        image="ghcr.io/example/agent@sha256:" + "a" * 64,
        mcp_image="ghcr.io/example/mcp@sha256:" + "b" * 64,
        command=[
            "agent",
            "{workspace}",
            "{prompt_file}",
            "{mcp_config}",
            "{result_file}",
        ],
        provider="example",
        model="coding-model",
        agent_version="1.0",
        evidence_type=AgentEvidenceType.REAL_CODING_AGENT,
    )


def _manifest(task_count: int) -> RealWorkloadManifest:
    repositories = [
        {
            "id": f"repo-{index}",
            "clone_url": f"https://github.com/example/repo-{index}.git",
            "source_url": f"https://github.com/example/repo-{index}",
            "license_spdx": "MIT",
            "license_url": f"https://github.com/example/repo-{index}/blob/main/LICENSE",
        }
        for index in range(min(3, max(1, task_count)))
    ]
    tasks = []
    for index in range(task_count):
        repository_id = f"repo-{index % 3}"
        tasks.append(
            {
                "id": f"task-{index:03d}",
                "repository_id": repository_id,
                "sequence_id": f"sequence-{index % 10}",
                "sequence_index": index // 10,
                "base_commit": f"{index + 1:040x}",
                "solution_commit": f"{index + 1001:040x}",
                "cutoff": "2025-02-01T00:00:00Z",
                "source_url": f"https://github.com/example/{repository_id}/issues/{index + 1}",
                "source_published_at": "2025-01-31T00:00:00Z",
                "prompt": f"Fix historical task {index}.",
                "hidden_test": {"image": HIDDEN_IMAGE, "command": ["python", "-m", "pytest"]},
            }
        )
    return RealWorkloadManifest.model_validate(
        {
            "name": "public-confirmatory",
            "tier": "public_replay",
            "generated_at": "2026-08-10T00:00:00Z",
            "repositories": repositories,
            "tasks": tasks,
        }
    )


def _records(manifest: RealWorkloadManifest) -> list[ConditionRunRecord]:
    records = []
    for task in manifest.tasks:
        for condition in ExperimentCondition:
            records.append(
                ConditionRunRecord(
                    task_id=task.id,
                    repository_id=task.repository_id,
                    sequence_id=task.sequence_id,
                    condition=condition,
                    execution_index=len(records),
                    prompt_sha256="d" * 64,
                    patch_sha256=f"{len(records) + 1:064x}",
                    execution_valid=True,
                    agent_completed=True,
                    memory_usage_valid=True,
                    hidden_test_success=condition is ExperimentCondition.MEMORYOS,
                    hidden_test_setup_valid=True,
                    cross_project_leaks=0,
                    stale_memory_uses=0,
                    memory_tool_calls=(0 if condition is ExperimentCondition.NO_MEMORY else 1),
                    retrieval_runs=(1 if condition is ExperimentCondition.MEMORYOS else 0),
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=0.01,
                    latency_seconds=1.0,
                )
            )
    return records


def test_dry_run_reports_paired_results_but_makes_no_effect_claim() -> None:
    manifest = _manifest(2)
    report = RealWorkloadReportBuilder().build(
        manifest,
        _runtime(),
        _records(manifest),
        mode=RunMode.DRY_RUN,
        run_id="dry-001",
        started_at=datetime.now(UTC),
    )

    assert report["status"] == "completed"
    assert report["effect_claim"] == "none"
    assert report["sample_size"] == 2
    comparison = report["paired_comparisons"]["no_memory_to_memoryos"]
    assert comparison["paired_n"] == 2
    assert comparison["metrics"]["functional_success"]["difference"] == 1.0
    assert report["aggregates"]["memoryos"]["retrieval_runs"] == 2


def test_confirmatory_protocol_requires_and_accepts_diverse_complete_sample() -> None:
    manifest = _manifest(50)
    started = datetime.now(UTC)
    report = RealWorkloadReportBuilder().build(
        manifest,
        _runtime(),
        _records(manifest),
        mode=RunMode.CONFIRMATORY,
        run_id="confirm-001",
        started_at=started,
        finished_at=started + timedelta(minutes=5),
    )

    assert report["protocol_valid"] is True
    assert report["protocol_errors"] == []
    assert report["effect_claim"] == "measured_on_this_registered_public_replay_protocol"
    assert report["sample_size"] == 50
    assert report["condition_run_count"] == 150


def test_prompt_mismatch_invalidates_protocol() -> None:
    manifest = _manifest(1)
    records = _records(manifest)
    records[1] = records[1].model_copy(update={"prompt_sha256": "e" * 64})

    report = RealWorkloadReportBuilder().build(
        manifest,
        _runtime(),
        records,
        mode=RunMode.DRY_RUN,
        run_id="dry-invalid",
        started_at=datetime.now(UTC),
    )

    assert report["status"] == "completed_invalid"
    assert report["effect_claim"] == "none"
    assert "non-identical prompts" in report["protocol_errors"][0]


def test_confirmatory_fixture_can_never_make_an_effect_claim() -> None:
    manifest = _manifest(50)
    runtime = _runtime().model_copy(
        update={"evidence_type": AgentEvidenceType.DETERMINISTIC_FIXTURE}
    )

    report = RealWorkloadReportBuilder().build(
        manifest,
        runtime,
        _records(manifest),
        mode=RunMode.CONFIRMATORY,
        run_id="confirm-fixture",
        started_at=datetime.now(UTC),
    )

    assert report["protocol_valid"] is False
    assert report["effect_claim"] == "none"
    assert "real_coding_agent" in " ".join(report["protocol_errors"])


def test_confirmatory_cross_project_leak_fails_the_safety_gate_and_effect_claim() -> None:
    manifest = _manifest(50)
    records = _records(manifest)
    memoryos_index = next(
        index
        for index, record in enumerate(records)
        if record.condition is ExperimentCondition.MEMORYOS
    )
    records[memoryos_index] = records[memoryos_index].model_copy(update={"cross_project_leaks": 1})

    report = RealWorkloadReportBuilder().build(
        manifest,
        _runtime(),
        records,
        mode=RunMode.CONFIRMATORY,
        run_id="confirm-leak",
        started_at=datetime.now(UTC),
    )

    assert report["protocol_valid"] is False
    assert report["safety_gate"]["passed"] is False
    assert report["effect_claim"] == "none"
    assert "zero cross-project leaks" in " ".join(report["protocol_errors"])
