from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memoryos.evaluation.metrics import bootstrap_mean_difference
from memoryos.evaluation.real_workload_agent import AgentEvidenceType
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    MemoryExpectation,
    MemorySeedSpec,
    RealWorkloadManifest,
    WorkloadTaskSpec,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AblationArm(StrEnum):
    MEMORYOS_FULL = "memoryos_full"
    MEMORYOS_MINUS_MEMORY = "memoryos_minus_memory"


class AblationEffectStatus(StrEnum):
    ESTIMATED = "estimated"
    NOT_SELECTED = "not_selected_in_full_run"
    NO_VALID_PAIRS = "no_valid_pairs"


class AblationPlanItem(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    repository_id: str
    excluded_memory_id: str
    expectation: Literal["helpful", "irrelevant"]
    reason: Literal["selected_eligible_memory"] = "selected_eligible_memory"


class ExecutableAblationRun(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=160)
    repository_id: str = Field(min_length=1, max_length=160)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_family: str = Field(min_length=1, max_length=160)
    agent_model: str = Field(min_length=1, max_length=300)
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeat_id: str = Field(min_length=1, max_length=160)
    evidence_type: AgentEvidenceType
    arm: AblationArm
    excluded_memory_id: str | None = Field(default=None, min_length=1, max_length=160)
    protocol_valid: bool
    agent_completed: bool
    hidden_test_success: bool
    selected_memory_ids: list[str] = Field(default_factory=list, max_length=500)
    candidate_traces: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=500)
    cross_project_leaks: int = Field(ge=0)
    stale_memory_uses: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_arm(self) -> ExecutableAblationRun:
        if len(set(self.selected_memory_ids)) != len(self.selected_memory_ids):
            raise ValueError("selected_memory_ids must be unique")
        if self.arm is AblationArm.MEMORYOS_FULL and self.excluded_memory_id is not None:
            raise ValueError("full-memory runs cannot exclude a memory")
        if self.arm is AblationArm.MEMORYOS_MINUS_MEMORY:
            if self.excluded_memory_id is None:
                raise ValueError("minus-memory runs require excluded_memory_id")
            if self.protocol_valid and self.excluded_memory_id in self.selected_memory_ids:
                raise ValueError("a valid ablation run cannot retrieve its excluded memory")
            if self.protocol_valid and self.excluded_memory_id in self.candidate_traces:
                raise ValueError("a valid ablation run cannot trace its excluded memory")
        return self

    @property
    def functional_success(self) -> bool:
        return self.agent_completed and self.hidden_test_success

    @property
    def safety_violations(self) -> int:
        return self.cross_project_leaks + self.stale_memory_uses


class PairedEstimate(StrictModel):
    difference: float
    ci95_low: float
    ci95_high: float


class MemoryAblationEffect(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    repository_id: str
    memory_id: str
    agent_family: str
    agent_model: str
    evidence_type: AgentEvidenceType
    status: AblationEffectStatus
    attempted_pairs: int = Field(ge=0)
    valid_pairs: int = Field(ge=0)
    informative_pairs: int = Field(ge=0)
    helped_pairs: int = Field(ge=0)
    harmed_pairs: int = Field(ge=0)
    unchanged_pairs: int = Field(ge=0)
    safety_worsened_pairs: int = Field(ge=0)
    success_effect: PairedEstimate | None = None
    latency_effect_seconds: PairedEstimate | None = None
    cost_effect_usd: PairedEstimate | None = None
    label_tier: Literal["executable_outcome"] = "executable_outcome"
    production_eligible: Literal[False] = False


class ExecutableAblationReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["executable_ablation_complete"] = "executable_ablation_complete"
    label_tier: Literal["executable_outcome"] = "executable_outcome"
    total_runs: int = Field(ge=0)
    full_runs: int = Field(ge=0)
    minus_runs: int = Field(ge=0)
    effects: list[MemoryAblationEffect]
    real_agent_effects: int = Field(ge=0)
    fixture_effects: int = Field(ge=0)
    safety_gate_passed: bool
    production_eligible: Literal[False] = False
    limitations: list[str]


def build_ablation_plan(
    manifest: RealWorkloadManifest,
    selected_seed_ids_by_task: dict[str, Sequence[str]],
) -> list[AblationPlanItem]:
    memories = {memory.id: memory for memory in manifest.memories}
    tasks = {task.id: task for task in manifest.tasks}
    unknown_tasks = sorted(set(selected_seed_ids_by_task) - set(tasks))
    if unknown_tasks:
        raise ValueError(f"ablation selections reference unknown tasks: {unknown_tasks}")
    plan: list[AblationPlanItem] = []
    for task_id, selected_ids in sorted(selected_seed_ids_by_task.items()):
        task = tasks[task_id]
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError(f"task {task_id} repeats a selected seed id")
        for memory_id in sorted(selected_ids):
            if memory_id not in task.memory_seed_ids:
                raise ValueError(f"task {task_id} did not register selected seed {memory_id}")
            memory = memories[memory_id]
            if memory.expectation not in {
                MemoryExpectation.HELPFUL,
                MemoryExpectation.IRRELEVANT,
            }:
                continue
            plan.append(
                AblationPlanItem(
                    task_id=task.id,
                    repository_id=task.repository_id,
                    excluded_memory_id=memory_id,
                    expectation=cast(Literal["helpful", "irrelevant"], memory.expectation.value),
                )
            )
    return plan


def materialize_ablation_manifest(
    manifest: RealWorkloadManifest,
    *,
    task_id: str,
    excluded_memory_id: str,
) -> RealWorkloadManifest:
    tasks = {task.id: task for task in manifest.tasks}
    memories = {memory.id: memory for memory in manifest.memories}
    try:
        task = tasks[task_id]
        excluded = memories[excluded_memory_id]
    except KeyError as exc:
        raise ValueError(f"unknown ablation identity: {exc.args[0]}") from exc
    if excluded_memory_id not in task.memory_seed_ids:
        raise ValueError("ablation memory is not registered for the task")
    if excluded.expectation not in {
        MemoryExpectation.HELPFUL,
        MemoryExpectation.IRRELEVANT,
    }:
        raise ValueError("safety-guard and stale memories are not utility-ablation targets")
    return materialize_task_manifest(
        manifest,
        task_id=task_id,
        excluded_memory_id=excluded_memory_id,
    )


def materialize_task_manifest(
    manifest: RealWorkloadManifest,
    *,
    task_id: str,
    excluded_memory_id: str | None = None,
) -> RealWorkloadManifest:
    tasks = {task.id: task for task in manifest.tasks}
    memories = {memory.id: memory for memory in manifest.memories}
    try:
        task = tasks[task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task identity: {task_id}") from exc
    if excluded_memory_id is not None and excluded_memory_id not in task.memory_seed_ids:
        raise ValueError("excluded memory is not registered for the task")
    retained_ids = [
        memory_id for memory_id in task.memory_seed_ids if memory_id != excluded_memory_id
    ]
    derived_task = task.model_copy(update={"memory_seed_ids": retained_ids})
    retained_memories = [memories[memory_id] for memory_id in retained_ids]
    repository_ids = {
        task.repository_id,
        *(memory.repository_id for memory in retained_memories),
    }
    repositories = [
        repository for repository in manifest.repositories if repository.id in repository_ids
    ]
    identity = hashlib.sha256(
        f"{manifest.digest()}\x1f{task_id}\x1f{excluded_memory_id or 'full'}".encode()
    ).hexdigest()[:16]
    payload = manifest.model_dump(mode="json", exclude_none=True)
    payload.update(
        {
            "name": f"ablation-{identity}",
            "repositories": [
                repository.model_dump(mode="json", exclude_none=True) for repository in repositories
            ],
            "memories": [
                memory.model_dump(mode="json", exclude_none=True) for memory in retained_memories
            ],
            "tasks": [derived_task.model_dump(mode="json", exclude_none=True)],
        }
    )
    return RealWorkloadManifest.model_validate(payload)


def ablation_run_from_report(
    report: dict[str, Any],
    task: WorkloadTaskSpec,
    *,
    arm: AblationArm,
    repeat_id: str,
    excluded_memory_id: str | None = None,
    expected_manifest_digest: str | None = None,
    expected_runtime_sha256: str | None = None,
    registered_memories: Sequence[MemorySeedSpec] = (),
) -> ExecutableAblationRun:
    if expected_manifest_digest is not None:
        manifest = report.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("digest") != expected_manifest_digest:
            raise ValueError("ablation report manifest digest does not match the expected arm")
    if expected_runtime_sha256 is not None and (
        report.get("runtime_spec_sha256") != expected_runtime_sha256
    ):
        raise ValueError("ablation report runtime digest does not match the expected runtime")
    records = [
        record
        for record in report.get("records", [])
        if record.get("task_id") == task.id
        and record.get("condition") == ExperimentCondition.MEMORYOS.value
    ]
    if len(records) != 1:
        raise ValueError("ablation conversion requires exactly one MemoryOS task record")
    record = records[0]
    runtime = report.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("real-workload report is missing runtime metadata")
    protocol_valid = bool(
        record.get("execution_valid")
        and record.get("memory_usage_valid")
        and record.get("hidden_test_setup_valid")
    )
    return ExecutableAblationRun(
        run_id=str(report.get("run_id", "")),
        task_id=task.id,
        repository_id=task.repository_id,
        base_commit=task.base_commit,
        prompt_sha256=str(record["prompt_sha256"]),
        agent_family=str(runtime["provider"]),
        agent_model=str(runtime["model"]),
        runtime_sha256=str(report.get("runtime_spec_sha256") or _canonical_hash(runtime)),
        repeat_id=repeat_id,
        evidence_type=AgentEvidenceType(str(runtime["evidence_type"])),
        arm=arm,
        excluded_memory_id=excluded_memory_id,
        protocol_valid=protocol_valid,
        agent_completed=bool(record.get("agent_completed")),
        hidden_test_success=bool(record.get("hidden_test_success")),
        selected_memory_ids=[str(value) for value in record.get("selected_seed_ids", [])],
        candidate_traces=_candidate_trace_index(record, registered_memories),
        cross_project_leaks=int(record.get("cross_project_leaks", 0)),
        stale_memory_uses=int(record.get("stale_memory_uses", 0)),
        input_tokens=_optional_int(record.get("input_tokens")),
        output_tokens=_optional_int(record.get("output_tokens")),
        cost_usd=_optional_float(record.get("cost_usd")),
        latency_seconds=float(record.get("latency_seconds", 0.0)),
    )


def analyze_executable_ablations(
    runs: Sequence[ExecutableAblationRun],
) -> ExecutableAblationReport:
    full_index: dict[tuple[str, str, str, str], ExecutableAblationRun] = {}
    minus_runs: list[ExecutableAblationRun] = []
    minus_keys: set[tuple[str, str, str, str, str]] = set()
    for run in runs:
        key = (run.task_id, run.agent_family, run.agent_model, run.repeat_id)
        if run.arm is AblationArm.MEMORYOS_FULL:
            if key in full_index:
                raise ValueError(f"duplicate full-memory ablation run: {key}")
            full_index[key] = run
        else:
            assert run.excluded_memory_id is not None
            minus_key = (*key, run.excluded_memory_id)
            if minus_key in minus_keys:
                raise ValueError(f"duplicate minus-memory ablation run: {minus_key}")
            minus_keys.add(minus_key)
            minus_runs.append(run)
    grouped: dict[
        tuple[str, str, str, str, str, AgentEvidenceType],
        list[tuple[ExecutableAblationRun, ExecutableAblationRun]],
    ] = defaultdict(list)
    for minus in minus_runs:
        match_key = (minus.task_id, minus.agent_family, minus.agent_model, minus.repeat_id)
        try:
            full = full_index[match_key]
        except KeyError as exc:
            raise ValueError(f"minus-memory run has no paired full run: {match_key}") from exc
        if (
            full.repository_id != minus.repository_id
            or full.base_commit != minus.base_commit
            or full.prompt_sha256 != minus.prompt_sha256
            or full.runtime_sha256 != minus.runtime_sha256
            or full.evidence_type is not minus.evidence_type
        ):
            raise ValueError(
                "ablation pair changed repository, commit, prompt, runtime, or evidence type"
            )
        assert minus.excluded_memory_id is not None
        group_key = (
            minus.task_id,
            minus.repository_id,
            minus.excluded_memory_id,
            minus.agent_family,
            minus.agent_model,
            minus.evidence_type,
        )
        grouped[group_key].append((full, minus))

    effects = [
        _effect_for_pairs(key, pairs)
        for key, pairs in sorted(grouped.items(), key=lambda item: tuple(map(str, item[0])))
    ]
    return ExecutableAblationReport(
        total_runs=len(runs),
        full_runs=len(full_index),
        minus_runs=len(minus_runs),
        effects=effects,
        real_agent_effects=sum(
            effect.evidence_type is AgentEvidenceType.REAL_CODING_AGENT
            and effect.status is AblationEffectStatus.ESTIMATED
            for effect in effects
        ),
        fixture_effects=sum(
            effect.evidence_type is AgentEvidenceType.DETERMINISTIC_FIXTURE
            and effect.status is AblationEffectStatus.ESTIMATED
            for effect in effects
        ),
        safety_gate_passed=all(effect.safety_worsened_pairs == 0 for effect in effects),
        limitations=[
            "Ablation estimates are local to the registered task, agent model, and repeat seeds.",
            "A memory not selected by the full run cannot receive an informative causal estimate.",
            "Fixture effects validate plumbing only and can never authorize production weights.",
            "Promotion requires repository-, time-, and agent-model-held-out executable outcomes.",
        ],
    )


def _effect_for_pairs(
    key: tuple[str, str, str, str, str, AgentEvidenceType],
    pairs: list[tuple[ExecutableAblationRun, ExecutableAblationRun]],
) -> MemoryAblationEffect:
    task_id, repository_id, memory_id, agent_family, agent_model, evidence_type = key
    valid = [(full, minus) for full, minus in pairs if full.protocol_valid and minus.protocol_valid]
    informative = [(full, minus) for full, minus in valid if memory_id in full.selected_memory_ids]
    if not valid:
        status = AblationEffectStatus.NO_VALID_PAIRS
    elif not informative:
        status = AblationEffectStatus.NOT_SELECTED
    else:
        status = AblationEffectStatus.ESTIMATED
    helped = sum(
        full.functional_success and not minus.functional_success for full, minus in informative
    )
    harmed = sum(
        not full.functional_success and minus.functional_success for full, minus in informative
    )
    unchanged = len(informative) - helped - harmed
    safety_worsened = sum(
        full.safety_violations > minus.safety_violations for full, minus in informative
    )
    seed = int.from_bytes(hashlib.sha256("\x1f".join(map(str, key)).encode()).digest()[:4], "big")
    success = _paired_estimate(
        [float(minus.functional_success) for _, minus in informative],
        [float(full.functional_success) for full, _ in informative],
        seed=seed,
    )
    latency = _paired_estimate(
        [minus.latency_seconds for _, minus in informative],
        [full.latency_seconds for full, _ in informative],
        seed=seed + 1,
    )
    cost_pairs = [
        (full, minus)
        for full, minus in informative
        if full.cost_usd is not None and minus.cost_usd is not None
    ]
    cost = _paired_estimate(
        [_required_float(minus.cost_usd) for _, minus in cost_pairs],
        [_required_float(full.cost_usd) for full, _ in cost_pairs],
        seed=seed + 2,
    )
    return MemoryAblationEffect(
        task_id=task_id,
        repository_id=repository_id,
        memory_id=memory_id,
        agent_family=agent_family,
        agent_model=agent_model,
        evidence_type=evidence_type,
        status=status,
        attempted_pairs=len(pairs),
        valid_pairs=len(valid),
        informative_pairs=len(informative),
        helped_pairs=helped,
        harmed_pairs=harmed,
        unchanged_pairs=unchanged,
        safety_worsened_pairs=safety_worsened,
        success_effect=success,
        latency_effect_seconds=latency,
        cost_effect_usd=cost,
    )


def _paired_estimate(
    baseline: list[float],
    treatment: list[float],
    *,
    seed: int,
) -> PairedEstimate | None:
    if not baseline:
        return None
    result = bootstrap_mean_difference(baseline, treatment, seed=seed)
    return PairedEstimate.model_validate(result)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(cast(Any, value))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(Any, value))


def _required_float(value: float | None) -> float:
    if value is None:  # pragma: no cover - caller filters missing costs
        raise ValueError("required numeric value is missing")
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_trace_index(
    record: dict[str, Any],
    registered_memories: Sequence[MemorySeedSpec] = (),
) -> dict[str, dict[str, Any]]:
    raw = record.get("retrieval_candidate_features", [])
    if not isinstance(raw, list):
        raise ValueError("retrieval_candidate_features must be a list")
    ordered = sorted(
        (item for item in raw if isinstance(item, dict)),
        key=lambda item: (
            not bool(item.get("selected")),
            int(item.get("retrieval_index", 0)),
            str(item.get("seed_id", "")),
        ),
    )
    indexed: dict[str, dict[str, Any]] = {}
    memory_index = {memory.id: memory for memory in registered_memories}
    for item in ordered:
        seed_id = str(item.get("seed_id", ""))
        trace = item.get("trace")
        if seed_id and seed_id not in indexed and isinstance(trace, dict):
            normalized = {str(key): value for key, value in trace.items()}
            memory = memory_index.get(seed_id)
            if memory is not None:
                _merge_registered_feature(
                    normalized,
                    "memory_confidence",
                    memory.confidence,
                )
                _merge_registered_feature(
                    normalized,
                    "memory_importance",
                    memory.importance,
                )
            indexed[seed_id] = normalized
    return indexed


def _merge_registered_feature(trace: dict[str, Any], name: str, expected: float) -> None:
    observed = trace.get(name)
    if observed is not None and float(observed) != expected:
        raise ValueError(f"retrieval trace {name} does not match the registered memory")
    trace[name] = expected


__all__ = [
    "AblationArm",
    "AblationEffectStatus",
    "AblationPlanItem",
    "ExecutableAblationReport",
    "ExecutableAblationRun",
    "MemoryAblationEffect",
    "PairedEstimate",
    "ablation_run_from_report",
    "analyze_executable_ablations",
    "build_ablation_plan",
    "materialize_ablation_manifest",
    "materialize_task_manifest",
]
