from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memoryos.evaluation.metrics import bootstrap_mean_difference
from memoryos.evaluation.real_workload_agent import AgentEvidenceType, AgentRuntimeSpec
from memoryos.evaluation.real_workload_models import (
    DatasetTier,
    ExperimentCondition,
    RealWorkloadManifest,
)


class RunMode(StrEnum):
    DRY_RUN = "dry_run"
    CONFIRMATORY = "confirmatory"


class ConditionRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    repository_id: str
    sequence_id: str
    condition: ExperimentCondition
    execution_index: int = Field(ge=0)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_valid: bool
    agent_completed: bool
    memory_usage_valid: bool
    hidden_test_success: bool
    hidden_test_setup_valid: bool
    cross_project_leaks: int = Field(ge=0)
    stale_memory_uses: int = Field(ge=0)
    selected_seed_ids: list[str] = Field(default_factory=list)
    memory_tool_calls: int = Field(ge=0)
    retrieval_runs: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_seconds: float = Field(ge=0)
    error_codes: list[str] = Field(default_factory=list)

    @field_validator("selected_seed_ids", "error_codes")
    @classmethod
    def require_unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("record list values must be unique")
        return value

    @property
    def protocol_valid(self) -> bool:
        return self.execution_valid and self.memory_usage_valid and self.hidden_test_setup_valid

    @property
    def functional_success(self) -> bool:
        return self.agent_completed and self.hidden_test_success


class RealWorkloadReportBuilder:
    def __init__(self, *, bootstrap_seed: int = 20260810) -> None:
        self.bootstrap_seed = bootstrap_seed

    def build(
        self,
        manifest: RealWorkloadManifest,
        runtime: AgentRuntimeSpec,
        records: list[ConditionRunRecord],
        *,
        mode: RunMode,
        run_id: str,
        started_at: datetime,
        finished_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError("real-workload report requires at least one run record")
        finished = finished_at or datetime.now(UTC)
        started = _aware_utc(started_at, field_name="started_at")
        finished = _aware_utc(finished, field_name="finished_at")
        if finished < started:
            raise ValueError("finished_at must not be earlier than started_at")
        indexed: dict[tuple[str, ExperimentCondition], ConditionRunRecord] = {}
        for record in records:
            key = (record.task_id, record.condition)
            if key in indexed:
                raise ValueError(
                    f"duplicate task condition record: {record.task_id}/{record.condition}"
                )
            indexed[key] = record
        manifest_tasks = {task.id: task for task in manifest.tasks}
        unknown = sorted({record.task_id for record in records} - set(manifest_tasks))
        if unknown:
            raise ValueError(f"records reference unknown manifest tasks: {unknown}")

        sampled_task_ids = sorted({record.task_id for record in records})
        protocol_errors = self._protocol_errors(
            manifest,
            runtime,
            indexed,
            sampled_task_ids,
            mode,
        )
        protocol_valid = not protocol_errors
        aggregates = {
            condition.value: _aggregate(
                [record for record in records if record.condition is condition]
            )
            for condition in ExperimentCondition
        }
        comparisons = self._comparisons(indexed, sampled_task_ids)
        memoryos_records = [
            record for record in records if record.condition is ExperimentCondition.MEMORYOS
        ]
        safety_gate = {
            "passed": all(record.cross_project_leaks == 0 for record in memoryos_records),
            "cross_project_leaks": sum(record.cross_project_leaks for record in memoryos_records),
            "required": "zero cross-project canary occurrences in the MemoryOS arm",
        }
        effect_claim = (
            "measured_on_this_registered_public_replay_protocol"
            if mode is RunMode.CONFIRMATORY and protocol_valid
            else "none"
        )
        return {
            "artifact_encoding": "utf-8; newline=LF",
            "schema_version": "2.2",
            "status": "completed" if protocol_valid else "completed_invalid",
            "run_id": run_id,
            "mode": mode.value,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "manifest": {
                "name": manifest.name,
                "tier": manifest.tier.value,
                "digest": manifest.digest(),
                "schema_version": manifest.schema_version,
            },
            "runtime": {
                "provider": runtime.provider,
                "model": runtime.model,
                "agent_version": runtime.agent_version,
                "evidence_type": runtime.evidence_type.value,
                "image": runtime.image,
                "mcp_image": runtime.mcp_image,
                "network_access": runtime.network_access.value,
                "user": runtime.user,
                "mcp_user": runtime.mcp_user,
                "scoring_user": runtime.scoring_user,
                "resources": {
                    "timeout_seconds": runtime.timeout_seconds,
                    "memory_mb": runtime.memory_mb,
                    "cpus": runtime.cpus,
                    "pids_limit": runtime.pids_limit,
                    "max_log_bytes": runtime.max_log_bytes,
                },
            },
            "sample_size": len(sampled_task_ids),
            "condition_run_count": len(records),
            "protocol_valid": protocol_valid,
            "protocol_errors": protocol_errors,
            "effect_claim": effect_claim,
            "safety_gate": safety_gate,
            "aggregates": aggregates,
            "paired_comparisons": comparisons,
            "records": [
                record.model_dump(mode="json")
                for record in sorted(records, key=lambda item: (item.task_id, item.condition.value))
            ],
            "truthfulness": (
                "Dry runs validate execution and scoring only; they make no effect claim."
                if mode is RunMode.DRY_RUN
                else "Effect claims require every confirmatory protocol gate to pass."
            ),
        }

    def _protocol_errors(
        self,
        manifest: RealWorkloadManifest,
        runtime: AgentRuntimeSpec,
        indexed: dict[tuple[str, ExperimentCondition], ConditionRunRecord],
        task_ids: list[str],
        mode: RunMode,
    ) -> list[str]:
        errors: list[str] = []
        expected_conditions = set(ExperimentCondition)
        for task_id in task_ids:
            task_records = {
                condition: indexed[(task_id, condition)]
                for condition in expected_conditions
                if (task_id, condition) in indexed
            }
            if set(task_records) != expected_conditions:
                errors.append(f"task {task_id} does not have all three conditions")
                continue
            prompt_hashes = {record.prompt_sha256 for record in task_records.values()}
            if len(prompt_hashes) != 1:
                errors.append(f"task {task_id} used non-identical prompts across conditions")
            for condition, record in task_records.items():
                if not record.protocol_valid:
                    errors.append(
                        f"task {task_id}/{condition.value} failed a protocol validity gate"
                    )
        if mode is RunMode.DRY_RUN:
            return sorted(set(errors))

        if manifest.tier is not DatasetTier.PUBLIC_REPLAY:
            errors.append("confirmatory mode requires a public_replay manifest")
        if runtime.evidence_type is not AgentEvidenceType.REAL_CODING_AGENT:
            errors.append("confirmatory mode requires a real_coding_agent runtime")
        if len(task_ids) < 50:
            errors.append("confirmatory mode requires at least 50 distinct tasks")
        repositories = {
            indexed[(task_id, ExperimentCondition.NO_MEMORY)].repository_id
            for task_id in task_ids
            if (task_id, ExperimentCondition.NO_MEMORY) in indexed
        }
        sequences = {
            indexed[(task_id, ExperimentCondition.NO_MEMORY)].sequence_id
            for task_id in task_ids
            if (task_id, ExperimentCondition.NO_MEMORY) in indexed
        }
        if len(repositories) < 3:
            errors.append("confirmatory mode requires at least 3 repositories")
        if len(sequences) < 10:
            errors.append("confirmatory mode requires at least 10 task sequences")
        if "@sha256:" not in runtime.image or "@sha256:" not in runtime.mcp_image:
            errors.append("confirmatory mode requires registry-qualified image digests")
        if runtime.network_access.value != "internal":
            errors.append("confirmatory mode forbids unrestricted agent internet egress")
        if any(
            record.condition is ExperimentCondition.MEMORYOS and record.cross_project_leaks > 0
            for record in indexed.values()
        ):
            errors.append("confirmatory mode requires zero cross-project leaks in the MemoryOS arm")
        for record in indexed.values():
            if (
                record.input_tokens is None
                or record.output_tokens is None
                or record.cost_usd is None
            ):
                errors.append("confirmatory mode requires complete token and cost accounting")
                break
        return sorted(set(errors))

    def _comparisons(
        self,
        indexed: dict[tuple[str, ExperimentCondition], ConditionRunRecord],
        task_ids: list[str],
    ) -> dict[str, Any]:
        pairs = [
            (ExperimentCondition.NO_MEMORY, ExperimentCondition.FLAT_MEMORY),
            (ExperimentCondition.NO_MEMORY, ExperimentCondition.MEMORYOS),
            (ExperimentCondition.FLAT_MEMORY, ExperimentCondition.MEMORYOS),
        ]
        result: dict[str, Any] = {}
        for pair_index, (baseline, treatment) in enumerate(pairs):
            paired = [
                (indexed[(task_id, baseline)], indexed[(task_id, treatment)])
                for task_id in task_ids
                if (task_id, baseline) in indexed
                and (task_id, treatment) in indexed
                and indexed[(task_id, baseline)].protocol_valid
                and indexed[(task_id, treatment)].protocol_valid
            ]
            key = f"{baseline.value}_to_{treatment.value}"
            if not paired:
                result[key] = {"paired_n": 0, "metrics": {}}
                continue
            metrics: dict[str, Any] = {}
            values = {
                "functional_success": (
                    [float(left.functional_success) for left, _ in paired],
                    [float(right.functional_success) for _, right in paired],
                ),
                "cross_project_leaks": (
                    [float(left.cross_project_leaks) for left, _ in paired],
                    [float(right.cross_project_leaks) for _, right in paired],
                ),
                "stale_memory_uses": (
                    [float(left.stale_memory_uses) for left, _ in paired],
                    [float(right.stale_memory_uses) for _, right in paired],
                ),
                "latency_seconds": (
                    [left.latency_seconds for left, _ in paired],
                    [right.latency_seconds for _, right in paired],
                ),
            }
            for metric_index, (name, (left, right)) in enumerate(values.items()):
                metrics[name] = bootstrap_mean_difference(
                    left,
                    right,
                    seed=self.bootstrap_seed + pair_index * 100 + metric_index,
                )
            cost_pairs = [
                (left.cost_usd, right.cost_usd)
                for left, right in paired
                if left.cost_usd is not None and right.cost_usd is not None
            ]
            if cost_pairs:
                metrics["cost_usd"] = bootstrap_mean_difference(
                    [float(left) for left, _ in cost_pairs],
                    [float(right) for _, right in cost_pairs],
                    seed=self.bootstrap_seed + pair_index * 100 + 50,
                )
                metrics["cost_usd"]["paired_n"] = len(cost_pairs)
            result[key] = {"paired_n": len(paired), "metrics": metrics}
        return result


def write_real_workload_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _aggregate(records: list[ConditionRunRecord]) -> dict[str, Any]:
    if not records:
        return {"runs": 0}
    costs = [record.cost_usd for record in records if record.cost_usd is not None]
    inputs = [record.input_tokens for record in records if record.input_tokens is not None]
    outputs = [record.output_tokens for record in records if record.output_tokens is not None]
    return {
        "runs": len(records),
        "protocol_valid_rate": sum(record.protocol_valid for record in records) / len(records),
        "functional_success_rate": sum(record.functional_success for record in records)
        / len(records),
        "cross_project_leaks": sum(record.cross_project_leaks for record in records),
        "stale_memory_uses": sum(record.stale_memory_uses for record in records),
        "mean_latency_seconds": sum(record.latency_seconds for record in records) / len(records),
        "mean_cost_usd": sum(costs) / len(costs) if costs else None,
        "total_input_tokens": sum(inputs) if inputs else None,
        "total_output_tokens": sum(outputs) if outputs else None,
        "memory_tool_calls": sum(record.memory_tool_calls for record in records),
        "retrieval_runs": sum(record.retrieval_runs for record in records),
    }


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return value.astimezone(UTC)


__all__ = [
    "ConditionRunRecord",
    "RealWorkloadReportBuilder",
    "RunMode",
    "write_real_workload_report",
]
