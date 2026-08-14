from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memoryos.evaluation.metrics import bootstrap_mean_difference
from memoryos.evaluation.real_workload_agent import AgentEvidenceType
from memoryos.evaluation.real_workload_models import (
    DatasetTier,
    ExperimentCondition,
    WorkloadTaskSpec,
)
from memoryos.retrieval_v2.pipeline import RRF_K, RRF_WEIGHTS, retrieval_config_hash
from memoryos.retrieval_v2.routing import (
    APPROVED_RETRIEVAL_RECIPES,
    RETRIEVAL_CHANNELS,
    SAFE_RECIPE_ID,
    RetrievalRoutingShadowProfile,
)


class RoutingCandidateEvaluation(BaseModel):
    """One paired production-baseline versus routing-shadow task observation."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=160)
    repository_id: str = Field(min_length=1, max_length=160)
    sequence_id: str = Field(min_length=1, max_length=160)
    repeat_id: str = Field(min_length=1, max_length=160)
    baseline_run_id: str = Field(min_length=1, max_length=200)
    candidate_run_id: str = Field(min_length=1, max_length=200)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    routing_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_model: str = Field(min_length=1, max_length=300)
    evidence_type: AgentEvidenceType
    dataset_tier: DatasetTier
    sealed: bool
    protocol_valid: bool
    baseline_success: bool
    candidate_success: bool
    baseline_safety_violations: int = Field(ge=0)
    candidate_safety_violations: int = Field(ge=0)
    baseline_latency_seconds: float = Field(ge=0.0)
    candidate_latency_seconds: float = Field(ge=0.0)
    baseline_cost_usd: float | None = Field(default=None, ge=0.0)
    candidate_cost_usd: float | None = Field(default=None, ge=0.0)
    baseline_selected_seed_ids: tuple[str, ...]
    candidate_selected_seed_ids: tuple[str, ...]
    recommended_recipe_counts: dict[str, int]
    executed_recipe_counts: dict[str, int]
    fallback_count: int = Field(ge=0)
    diagnostic_only: Literal[True] = True
    production_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_recipe_accounting(self) -> RoutingCandidateEvaluation:
        allowed = set(APPROVED_RETRIEVAL_RECIPES)
        for label, counts in (
            ("recommended", self.recommended_recipe_counts),
            ("executed", self.executed_recipe_counts),
        ):
            if not counts:
                raise ValueError(f"{label} recipe counts cannot be empty")
            if not set(counts).issubset(allowed):
                raise ValueError(f"{label} recipe counts contain an unapproved recipe")
            if any(count <= 0 for count in counts.values()):
                raise ValueError(f"{label} recipe counts must be positive")
        if self.recommended_recipe_counts != self.executed_recipe_counts:
            raise ValueError("candidate execution must match its recommended recipes")
        if self.fallback_count != self.recommended_recipe_counts.get(SAFE_RECIPE_ID, 0):
            raise ValueError("fallback count must match safe-fallback recommendations")
        if self.baseline_config_sha256 == self.candidate_config_sha256:
            raise ValueError("baseline and routing candidate configs must differ")
        if self.baseline_run_id == self.candidate_run_id:
            raise ValueError("baseline and routing candidate run IDs must differ")
        if self.baseline_report_sha256 == self.candidate_report_sha256:
            raise ValueError("baseline and routing candidate reports must differ")
        if len(set(self.baseline_selected_seed_ids)) != len(self.baseline_selected_seed_ids):
            raise ValueError("baseline selected seed IDs must be unique")
        if len(set(self.candidate_selected_seed_ids)) != len(self.candidate_selected_seed_ids):
            raise ValueError("candidate selected seed IDs must be unique")
        return self


class RoutingPromotionProtocol(BaseModel):
    """Named policy gates; these values are not retrieval-score parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["1.0"] = "1.0"
    policy_provenance: Literal["inherited_from_executable_weight_promotion_v1"] = (
        "inherited_from_executable_weight_promotion_v1"
    )
    min_tasks: int = Field(default=50, ge=1, le=100_000)
    min_repositories: int = Field(default=3, ge=1, le=1000)
    min_sequences: int = Field(default=10, ge=1, le=100_000)
    min_agent_models: int = Field(default=2, ge=1, le=100)
    min_tasks_per_agent_model: int = Field(default=10, ge=1, le=100_000)
    min_distinct_recipes: int = Field(
        default=4,
        ge=1,
        le=len(APPROVED_RETRIEVAL_RECIPES),
    )
    min_tasks_per_recipe: int = Field(default=5, ge=1, le=100_000)
    success_ci95_low_must_exceed: float = Field(default=0.0, ge=-1.0, le=1.0)
    max_latency_increase_ratio: float = Field(default=0.25, ge=0.0, le=10.0)
    max_cost_increase_ratio: float = Field(default=0.25, ge=0.0, le=10.0)
    require_complete_agent_task_matrix: bool = True
    require_balanced_repeat_matrix: bool = True
    require_nonnegative_worst_repository_delta: bool = True
    require_nonnegative_worst_agent_delta: bool = True
    require_nonnegative_worst_recipe_delta: bool = True
    reject_any_task_safety_regression: bool = True
    require_complete_cost_accounting: bool = True

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RoutingPromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["retain_frozen_baseline", "eligible_for_sealed_activation_review"]
    routing_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_seed: int = Field(ge=0)
    task_count: int = Field(ge=0)
    repository_count: int = Field(ge=0)
    sequence_count: int = Field(ge=0)
    agent_model_count: int = Field(ge=0)
    recipe_count: int = Field(ge=0)
    recipe_task_counts: dict[str, int]
    success_difference: dict[str, float] | None
    repository_success_deltas: dict[str, float]
    agent_success_deltas: dict[str, float]
    recipe_success_deltas: dict[str, float]
    worst_repository_success_delta: float | None
    worst_agent_success_delta: float | None
    worst_recipe_success_delta: float | None
    safety_regression_count: int = Field(ge=0)
    latency_increase_ratio: float | None
    cost_increase_ratio: float | None
    fallback_rate: float = Field(ge=0.0, le=1.0)
    reasons: tuple[str, ...]
    production_activated: Literal[False] = False


def routing_evaluation_from_reports(
    profile: RetrievalRoutingShadowProfile,
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
    task: WorkloadTaskSpec,
    *,
    repeat_id: str,
) -> RoutingCandidateEvaluation:
    """Validate a paired run and extract a non-promotional routing observation."""

    baseline = _single_memoryos_record(baseline_report, task.id)
    candidate = _single_memoryos_record(candidate_report, task.id)
    baseline_manifest = baseline_report.get("manifest")
    if not isinstance(baseline_manifest, dict) or baseline_manifest != candidate_report.get(
        "manifest"
    ):
        raise ValueError("routing shadow pair changed or omitted the task manifest")
    manifest_sha256 = str(baseline_manifest.get("digest", ""))
    if not _is_sha256(manifest_sha256):
        raise ValueError("routing shadow pair omitted the manifest digest")
    if (
        baseline.get("repository_id") != task.repository_id
        or candidate.get("repository_id") != task.repository_id
        or baseline.get("sequence_id") != task.sequence_id
        or candidate.get("sequence_id") != task.sequence_id
    ):
        raise ValueError("routing shadow records do not match the registered task")
    baseline_run_id = str(baseline_report.get("run_id", ""))
    candidate_run_id = str(candidate_report.get("run_id", ""))
    if not baseline_run_id or not candidate_run_id or baseline_run_id == candidate_run_id:
        raise ValueError("baseline and routing candidate must come from distinct runs")
    if baseline.get("prompt_sha256") != candidate.get("prompt_sha256"):
        raise ValueError("routing shadow pair changed the task prompt")

    runtime_sha256 = str(baseline_report.get("runtime_spec_sha256", ""))
    if (
        not _is_sha256(runtime_sha256)
        or candidate_report.get("runtime_spec_sha256") != runtime_sha256
    ):
        raise ValueError("routing shadow pair changed or omitted the pinned agent runtime")
    baseline_runtime = baseline_report.get("runtime")
    candidate_runtime = candidate_report.get("runtime")
    if not isinstance(baseline_runtime, dict) or baseline_runtime != candidate_runtime:
        raise ValueError("routing shadow pair changed runtime metadata")

    profile_digest = profile.digest()
    if any(
        value is not None
        for value in (
            baseline_report.get("scoring_profile_sha256"),
            candidate_report.get("scoring_profile_sha256"),
            baseline.get("scoring_profile_sha256"),
            candidate.get("scoring_profile_sha256"),
        )
    ):
        raise ValueError("routing shadow pair cannot compose a scoring profile")
    if (
        baseline_report.get("routing_profile_sha256") is not None
        or baseline.get("routing_profile_sha256") is not None
    ):
        raise ValueError("routing shadow baseline unexpectedly used a routing profile")
    if (
        candidate_report.get("routing_profile_sha256") != profile_digest
        or candidate.get("routing_profile_sha256") != profile_digest
    ):
        raise ValueError("candidate report did not bind the expected routing profile")

    baseline_config = _one_config_hash(baseline)
    candidate_config = _one_config_hash(candidate)
    expected_baseline_config = retrieval_config_hash()
    expected_candidate_config = retrieval_config_hash(routing_profile=profile)
    if baseline_config != expected_baseline_config:
        raise ValueError("baseline report did not execute the frozen production config")
    if candidate_config != expected_candidate_config:
        raise ValueError("candidate report did not execute the expected routing config")

    baseline_routes = _routing_evidence(baseline)
    candidate_routes = _routing_evidence(candidate)
    for route in baseline_routes:
        _validate_route(route, profile=profile, candidate=False)
    for route in candidate_routes:
        _validate_route(route, profile=profile, candidate=True)

    prompt_sha256 = str(baseline["prompt_sha256"])
    identity = hashlib.sha256(
        "\x1f".join(
            [
                task.id,
                str(baseline_runtime.get("model", "")),
                repeat_id,
                prompt_sha256,
                profile_digest,
            ]
        ).encode()
    ).hexdigest()[:40]
    recommended = Counter(str(route["recommended_recipe_id"]) for route in candidate_routes)
    executed = Counter(str(route["executed_recipe_id"]) for route in candidate_routes)
    return RoutingCandidateEvaluation(
        evaluation_id=f"routing-shadow-{identity}",
        task_id=task.id,
        repository_id=task.repository_id,
        sequence_id=task.sequence_id,
        repeat_id=repeat_id,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        base_commit=task.base_commit,
        manifest_sha256=manifest_sha256,
        task_spec_sha256=_canonical_sha256(task.model_dump(mode="json")),
        baseline_report_sha256=_canonical_sha256(baseline_report),
        candidate_report_sha256=_canonical_sha256(candidate_report),
        routing_profile_sha256=profile_digest,
        recipe_registry_sha256=profile.recipe_registry_sha256,
        baseline_config_sha256=baseline_config,
        candidate_config_sha256=candidate_config,
        prompt_sha256=prompt_sha256,
        runtime_sha256=runtime_sha256,
        agent_model=str(baseline_runtime.get("model", "")),
        evidence_type=AgentEvidenceType(str(baseline_runtime.get("evidence_type", ""))),
        dataset_tier=DatasetTier(str(baseline_manifest.get("tier", ""))),
        sealed=bool(
            baseline_manifest.get("tier") == DatasetTier.PUBLIC_REPLAY.value
            and task.solution_commit is not None
            and task.source_published_at is not None
            and task.hidden_test.hidden_patch_sha256 is not None
        ),
        protocol_valid=_record_protocol_valid(baseline) and _record_protocol_valid(candidate),
        baseline_success=bool(
            baseline.get("agent_completed") and baseline.get("hidden_test_success")
        ),
        candidate_success=bool(
            candidate.get("agent_completed") and candidate.get("hidden_test_success")
        ),
        baseline_safety_violations=int(baseline.get("cross_project_leaks", 0))
        + int(baseline.get("stale_memory_uses", 0)),
        candidate_safety_violations=int(candidate.get("cross_project_leaks", 0))
        + int(candidate.get("stale_memory_uses", 0)),
        baseline_latency_seconds=float(baseline.get("latency_seconds", 0.0)),
        candidate_latency_seconds=float(candidate.get("latency_seconds", 0.0)),
        baseline_cost_usd=_optional_float(baseline.get("cost_usd")),
        candidate_cost_usd=_optional_float(candidate.get("cost_usd")),
        baseline_selected_seed_ids=_selected_seed_ids(baseline),
        candidate_selected_seed_ids=_selected_seed_ids(candidate),
        recommended_recipe_counts=dict(sorted(recommended.items())),
        executed_recipe_counts=dict(sorted(executed.items())),
        fallback_count=sum(bool(route.get("fallback_used")) for route in candidate_routes),
    )


def evaluate_routing_candidate(
    profile: RetrievalRoutingShadowProfile,
    evaluations: Sequence[RoutingCandidateEvaluation],
    *,
    protocol: RoutingPromotionProtocol | None = None,
    bootstrap_seed: int = 20260813,
) -> RoutingPromotionDecision:
    """Aggregate task-level causal outcomes and fail closed on every incomplete slice."""

    if (
        isinstance(bootstrap_seed, bool)
        or not isinstance(bootstrap_seed, int)
        or bootstrap_seed < 0
    ):
        raise ValueError("bootstrap seed must be a non-negative integer")
    evaluations = tuple(
        RoutingCandidateEvaluation.model_validate(item.model_dump(mode="json"))
        for item in evaluations
    )
    configured = protocol or RoutingPromotionProtocol()
    reasons: list[str] = []
    profile_digest = profile.digest()
    evaluation_set_sha256 = routing_evaluation_set_digest(evaluations)
    if not evaluations:
        reasons.append("routing promotion requires paired evaluations")
    if len({item.evaluation_id for item in evaluations}) != len(evaluations):
        reasons.append("routing evaluation IDs must be unique")
    comparison_keys = {(item.task_id, item.agent_model, item.repeat_id) for item in evaluations}
    if len(comparison_keys) != len(evaluations):
        reasons.append("task/agent/repeat routing comparisons must be unique")
    if len({item.baseline_run_id for item in evaluations}) != len(evaluations) or len(
        {item.candidate_run_id for item in evaluations}
    ) != len(evaluations):
        reasons.append("every routing comparison must use distinct arm run IDs")
    if len({item.baseline_report_sha256 for item in evaluations}) != len(evaluations) or len(
        {item.candidate_report_sha256 for item in evaluations}
    ) != len(evaluations):
        reasons.append("every routing comparison must use distinct arm reports")
    if any(item.routing_profile_sha256 != profile_digest for item in evaluations):
        reasons.append("every routing evaluation must bind the candidate profile")
    if any(item.recipe_registry_sha256 != profile.recipe_registry_sha256 for item in evaluations):
        reasons.append("every routing evaluation must bind the recipe registry")
    expected_baseline_config = retrieval_config_hash()
    expected_candidate_config = retrieval_config_hash(routing_profile=profile)
    if any(item.baseline_config_sha256 != expected_baseline_config for item in evaluations):
        reasons.append("every routing evaluation must bind the frozen baseline config")
    if any(item.candidate_config_sha256 != expected_candidate_config for item in evaluations):
        reasons.append("every routing evaluation must bind the routing candidate config")
    if any(not item.protocol_valid for item in evaluations):
        reasons.append("every routing evaluation must pass protocol validation")
    if any(not item.sealed for item in evaluations):
        reasons.append("every routing promotion task must have sealed public provenance")
    if any(item.dataset_tier is not DatasetTier.PUBLIC_REPLAY for item in evaluations):
        reasons.append("routing promotion accepts public-replay tasks only")
    if any(item.evidence_type is not AgentEvidenceType.REAL_CODING_AGENT for item in evaluations):
        reasons.append("routing promotion accepts real coding-agent evidence only")

    tasks = sorted({item.task_id for item in evaluations})
    repositories = sorted({item.repository_id for item in evaluations})
    sequences = sorted({item.sequence_id for item in evaluations})
    agent_models = sorted({item.agent_model for item in evaluations})
    task_groups: dict[str, list[RoutingCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        task_groups[item.task_id].append(item)
    identity_fields = (
        "repository_id",
        "sequence_id",
        "base_commit",
        "manifest_sha256",
        "task_spec_sha256",
        "prompt_sha256",
    )
    if any(
        len({getattr(item, field) for item in items}) != 1
        for items in task_groups.values()
        for field in identity_fields
    ):
        reasons.append("task identity and provenance must be invariant across agent runs")
    if any(
        len({item.runtime_sha256 for item in evaluations if item.agent_model == model}) != 1
        for model in agent_models
    ):
        reasons.append("each agent model must use one pinned runtime")
    if any(
        len({item.agent_model for item in evaluations if item.runtime_sha256 == runtime_sha256})
        != 1
        for runtime_sha256 in {item.runtime_sha256 for item in evaluations}
    ):
        reasons.append("one runtime identity cannot claim multiple agent models")
    if len(tasks) < configured.min_tasks:
        reasons.append(f"requires at least {configured.min_tasks} sealed tasks")
    if len(repositories) < configured.min_repositories:
        reasons.append(f"requires at least {configured.min_repositories} repositories")
    if len(sequences) < configured.min_sequences:
        reasons.append(f"requires at least {configured.min_sequences} task sequences")
    if len(agent_models) < configured.min_agent_models:
        reasons.append(f"requires at least {configured.min_agent_models} agent models")
    tasks_by_agent = {
        model: {item.task_id for item in evaluations if item.agent_model == model}
        for model in agent_models
    }
    if any(
        len(model_tasks) < configured.min_tasks_per_agent_model
        for model_tasks in tasks_by_agent.values()
    ):
        reasons.append(
            f"each agent model requires at least {configured.min_tasks_per_agent_model} tasks"
        )
    if configured.require_complete_agent_task_matrix:
        expected_agents = set(agent_models)
        if any(
            {item.agent_model for item in evaluations if item.task_id == task_id} != expected_agents
            for task_id in tasks
        ):
            reasons.append("every routing task must run on every promotion agent model")
    if configured.require_balanced_repeat_matrix and tasks and agent_models:
        repeat_sets = [
            {
                item.repeat_id
                for item in evaluations
                if item.task_id == task_id and item.agent_model == model
            }
            for task_id in tasks
            for model in agent_models
        ]
        if (
            not repeat_sets
            or not repeat_sets[0]
            or any(repeat_ids != repeat_sets[0] for repeat_ids in repeat_sets[1:])
        ):
            reasons.append("every task/agent cell must use the same repeat IDs")

    recipe_tasks: dict[str, set[str]] = defaultdict(set)
    for item in evaluations:
        for recipe_id, count in item.recommended_recipe_counts.items():
            if count > 0:
                recipe_tasks[recipe_id].add(item.task_id)
    recipe_task_counts = {
        recipe_id: len(task_ids) for recipe_id, task_ids in sorted(recipe_tasks.items())
    }
    if len(recipe_task_counts) < configured.min_distinct_recipes:
        reasons.append(f"requires at least {configured.min_distinct_recipes} routed recipe slices")
    if any(count < configured.min_tasks_per_recipe for count in recipe_task_counts.values()):
        reasons.append(
            f"each observed recipe requires at least {configured.min_tasks_per_recipe} tasks"
        )

    task_baseline, task_candidate = _paired_task_values(evaluations)
    success_difference = (
        bootstrap_mean_difference(
            task_baseline,
            task_candidate,
            seed=bootstrap_seed,
        )
        if task_baseline
        else None
    )
    if (
        success_difference is None
        or success_difference["ci95_low"] <= configured.success_ci95_low_must_exceed
    ):
        reasons.append("task-level success lower confidence bound is not positive")

    repository_deltas = _slice_success_deltas(evaluations, "repository_id")
    agent_deltas = _slice_success_deltas(evaluations, "agent_model")
    recipe_deltas = _recipe_success_deltas(evaluations, recipe_tasks)
    worst_repository = min(repository_deltas.values()) if repository_deltas else None
    worst_agent = min(agent_deltas.values()) if agent_deltas else None
    worst_recipe = min(recipe_deltas.values()) if recipe_deltas else None
    if configured.require_nonnegative_worst_repository_delta and worst_repository is None:
        reasons.append("worst-repository safety slice is missing")
    if (
        configured.require_nonnegative_worst_repository_delta
        and worst_repository is not None
        and worst_repository < 0.0
    ):
        reasons.append("worst-repository success regressed")
    if configured.require_nonnegative_worst_agent_delta and worst_agent is None:
        reasons.append("worst-agent safety slice is missing")
    elif (
        configured.require_nonnegative_worst_agent_delta
        and worst_agent is not None
        and worst_agent < 0.0
    ):
        reasons.append("worst-agent success regressed")
    if configured.require_nonnegative_worst_recipe_delta and worst_recipe is None:
        reasons.append("worst-recipe safety slice is missing")
    elif (
        configured.require_nonnegative_worst_recipe_delta
        and worst_recipe is not None
        and worst_recipe < 0.0
    ):
        reasons.append("worst-recipe success regressed")

    safety_regressions = sum(
        item.candidate_safety_violations > item.baseline_safety_violations for item in evaluations
    )
    if configured.reject_any_task_safety_regression and safety_regressions:
        reasons.append("at least one routing task regressed on safety")
    latency_ratio = _increase_ratio(
        [item.baseline_latency_seconds for item in evaluations],
        [item.candidate_latency_seconds for item in evaluations],
    )
    if latency_ratio is None or latency_ratio > configured.max_latency_increase_ratio:
        reasons.append("routing candidate exceeded the latency budget")
    costs_complete = all(
        item.baseline_cost_usd is not None and item.candidate_cost_usd is not None
        for item in evaluations
    )
    baseline_costs = [
        float(item.baseline_cost_usd) for item in evaluations if item.baseline_cost_usd is not None
    ]
    candidate_costs = [
        float(item.candidate_cost_usd)
        for item in evaluations
        if item.candidate_cost_usd is not None
    ]
    cost_ratio = _increase_ratio(baseline_costs, candidate_costs) if costs_complete else None
    if configured.require_complete_cost_accounting and not costs_complete:
        reasons.append("routing promotion requires complete paired cost accounting")
    elif cost_ratio is None or cost_ratio > configured.max_cost_increase_ratio:
        reasons.append("routing candidate exceeded the cost budget")

    total_route_calls = sum(sum(item.recommended_recipe_counts.values()) for item in evaluations)
    fallback_rate = (
        sum(item.fallback_count for item in evaluations) / total_route_calls
        if total_route_calls
        else 0.0
    )
    unique_reasons = tuple(dict.fromkeys(reasons))
    return RoutingPromotionDecision(
        status=(
            "eligible_for_sealed_activation_review"
            if not unique_reasons
            else "retain_frozen_baseline"
        ),
        routing_profile_sha256=profile_digest,
        recipe_registry_sha256=profile.recipe_registry_sha256,
        promotion_protocol_sha256=configured.digest(),
        evaluation_set_sha256=evaluation_set_sha256,
        bootstrap_seed=bootstrap_seed,
        task_count=len(tasks),
        repository_count=len(repositories),
        sequence_count=len(sequences),
        agent_model_count=len(agent_models),
        recipe_count=len(recipe_task_counts),
        recipe_task_counts=recipe_task_counts,
        success_difference=success_difference,
        repository_success_deltas=repository_deltas,
        agent_success_deltas=agent_deltas,
        recipe_success_deltas=recipe_deltas,
        worst_repository_success_delta=worst_repository,
        worst_agent_success_delta=worst_agent,
        worst_recipe_success_delta=worst_recipe,
        safety_regression_count=safety_regressions,
        latency_increase_ratio=latency_ratio,
        cost_increase_ratio=cost_ratio,
        fallback_rate=fallback_rate,
        reasons=unique_reasons,
    )


def _validate_route(
    route: dict[str, Any],
    *,
    profile: RetrievalRoutingShadowProfile,
    candidate: bool,
) -> None:
    expected_mode = "candidate_shadow" if candidate else "frozen_production_baseline"
    if route.get("execution_mode") != expected_mode:
        raise ValueError(f"routing evidence did not execute {expected_mode}")
    if route.get("router_version") != profile.router_version:
        raise ValueError("routing evidence used an unexpected router version")
    if route.get("decision_basis") not in {
        "explicit_signals",
        "planner_intent",
        "safe_fallback",
    }:
        raise ValueError("routing evidence has an invalid decision basis")
    reason_codes = route.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or not all(isinstance(value, str) and value for value in reason_codes)
    ):
        raise ValueError("routing evidence requires stable reason codes")
    features = route.get("features")
    expected_feature_keys = {
        "intent_reason_code",
        "exact_term_count",
        "entity_count",
        "has_exact_signal",
        "has_relational_signal",
        "has_temporal_signal",
        "clause_count",
    }
    if not isinstance(features, dict) or set(features) != expected_feature_keys:
        raise ValueError("routing evidence has an incomplete feature record")
    intent_reason_code = features["intent_reason_code"]
    if not isinstance(intent_reason_code, str) or not intent_reason_code:
        raise ValueError("routing intent reason code is invalid")
    for count_field in ("exact_term_count", "entity_count", "clause_count"):
        count = features[count_field]
        minimum = 1 if count_field == "clause_count" else 0
        if isinstance(count, bool) or not isinstance(count, int) or count < minimum:
            raise ValueError("routing feature count is invalid")
    for flag_field in (
        "has_exact_signal",
        "has_relational_signal",
        "has_temporal_signal",
    ):
        if not isinstance(features[flag_field], bool):
            raise ValueError("routing feature flag is invalid")

    recommended_id = route.get("recommended_recipe_id")
    executed_id = route.get("executed_recipe_id")
    if not isinstance(recommended_id, str) or recommended_id not in profile.allowed_recipe_ids:
        raise ValueError("routing evidence recommended an unapproved recipe")
    fallback_used = route.get("fallback_used")
    if not isinstance(fallback_used, bool) or fallback_used != (recommended_id == SAFE_RECIPE_ID):
        raise ValueError("routing evidence fallback flag does not match its recommendation")
    if fallback_used != (route.get("decision_basis") == "safe_fallback"):
        raise ValueError("routing decision basis does not match its fallback state")
    expected_reason_codes = {
        SAFE_RECIPE_ID: {"unclassified_safe_fallback"},
        "exact-symbol-v1": {"exact_identifier_or_location"},
        "semantic-hybrid-v1": {"semantic_intent"},
        "relational-graph-v1": {"relationship_or_provenance"},
        "temporal-as-of-v1": {"temporal_signal"},
        "complex-hybrid-v1": {"multi_signal_or_clause"},
    }
    if set(reason_codes) != expected_reason_codes[recommended_id]:
        raise ValueError("routing reason codes do not match the recommended recipe")
    feature_signal_count = sum(
        bool(features[field])
        for field in (
            "has_exact_signal",
            "has_relational_signal",
            "has_temporal_signal",
        )
    )
    route_feature_valid = {
        SAFE_RECIPE_ID: feature_signal_count == 0,
        "exact-symbol-v1": bool(features["has_exact_signal"]),
        "semantic-hybrid-v1": feature_signal_count == 0,
        "relational-graph-v1": bool(features["has_relational_signal"]),
        "temporal-as-of-v1": bool(features["has_temporal_signal"]),
        "complex-hybrid-v1": feature_signal_count > 1 or int(features["clause_count"]) > 1,
    }[recommended_id]
    if not route_feature_valid:
        raise ValueError("routing features do not support the recommended recipe")
    expected_executed_id = recommended_id if candidate else SAFE_RECIPE_ID
    if executed_id != expected_executed_id:
        raise ValueError("routing evidence did not execute the required recipe")
    recipe = APPROVED_RETRIEVAL_RECIPES[expected_executed_id]
    if (
        route.get("recommended_recipe_sha256")
        != APPROVED_RETRIEVAL_RECIPES[recommended_id].digest()
    ):
        raise ValueError("routing evidence has an invalid recommended recipe digest")
    if route.get("executed_recipe_sha256") != recipe.digest():
        raise ValueError("routing evidence has an invalid executed recipe digest")
    if route.get("route") != APPROVED_RETRIEVAL_RECIPES[recommended_id].route.value:
        raise ValueError("routing evidence route does not match its recommendation")
    if route.get("active_channels") != list(recipe.channels):
        raise ValueError("routing evidence active channels do not match the recipe")
    if route.get("requested_channels") != list(recipe.channels):
        raise ValueError("routing evidence requested channels do not match the recipe")
    executions = route.get("channel_execution")
    if not isinstance(executions, list) or len(executions) != len(RETRIEVAL_CHANNELS):
        raise ValueError("routing evidence must report every channel capability")
    if [
        execution.get("channel") for execution in executions if isinstance(execution, dict)
    ] != list(RETRIEVAL_CHANNELS):
        raise ValueError("routing channel execution must use canonical channel order")
    execution_by_channel: dict[str, dict[str, Any]] = {}
    for execution in executions:
        if not isinstance(execution, dict):
            raise ValueError("routing channel execution must be an object")
        channel = execution.get("channel")
        if not isinstance(channel, str) or channel in execution_by_channel:
            raise ValueError("routing channel execution has a duplicate or invalid channel")
        execution_by_channel[channel] = execution
    if set(execution_by_channel) != set(RETRIEVAL_CHANNELS):
        raise ValueError("routing channel execution does not cover the canonical registry")
    allowed_statuses = {
        "not_requested",
        "not_applicable",
        "executed",
        "executed_empty",
        "unavailable",
        "provider_fallback",
    }
    for channel in RETRIEVAL_CHANNELS:
        execution = execution_by_channel[channel]
        if execution.get("requested") != (channel in recipe.channels):
            raise ValueError("routing channel requested flag does not match the recipe")
        for flag_field in ("requested", "available", "attempted", "executed"):
            if not isinstance(execution.get(flag_field), bool):
                raise ValueError("routing channel execution has an invalid boolean flag")
        for count_field in ("candidate_count", "eligible_candidate_count"):
            count = execution.get(count_field)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("routing channel execution has an invalid candidate count")
        if int(execution["eligible_candidate_count"]) > int(execution["candidate_count"]):
            raise ValueError("eligible routing candidates cannot exceed raw candidates")
        status = execution.get("status")
        if status not in allowed_statuses:
            raise ValueError("routing channel execution has an invalid status")
        reason_code = execution.get("reason_code")
        if reason_code is not None and (not isinstance(reason_code, str) or not reason_code):
            raise ValueError("routing channel execution has an invalid reason code")
        requested = bool(execution["requested"])
        available = bool(execution["available"])
        attempted = bool(execution["attempted"])
        executed = bool(execution["executed"])
        raw_count = int(execution["candidate_count"])
        eligible_count = int(execution["eligible_candidate_count"])
        if status == "not_requested" and (
            requested or attempted or executed or raw_count or eligible_count
        ):
            raise ValueError("not-requested channel evidence is inconsistent")
        if status == "not_applicable" and (
            not requested or attempted or executed or raw_count or eligible_count
        ):
            raise ValueError("not-applicable channel evidence is inconsistent")
        if status == "unavailable" and (
            not requested or available or attempted or executed or raw_count or eligible_count
        ):
            raise ValueError("unavailable channel evidence is inconsistent")
        if status == "provider_fallback" and (
            not requested
            or not available
            or not attempted
            or executed
            or raw_count
            or eligible_count
        ):
            raise ValueError("provider-fallback channel evidence is inconsistent")
        if status in {"executed", "executed_empty"} and (
            not requested or not available or not attempted or not executed
        ):
            raise ValueError("executed channel evidence is inconsistent")
        if status == "executed" and eligible_count <= 0:
            raise ValueError("executed channel must contribute an eligible candidate")
        if status == "executed_empty" and eligible_count != 0:
            raise ValueError("executed-empty channel cannot contribute candidates")
        duration = execution.get("duration_ms")
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0.0
        ):
            raise ValueError("routing channel execution has an invalid duration")
    executed_channels = [
        channel for channel in RETRIEVAL_CHANNELS if execution_by_channel[channel].get("executed")
    ]
    contributing_channels = [
        channel
        for channel in RETRIEVAL_CHANNELS
        if execution_by_channel[channel].get("executed")
        and int(execution_by_channel[channel]["eligible_candidate_count"]) > 0
    ]
    degraded_channels = [
        channel
        for channel in RETRIEVAL_CHANNELS
        if execution_by_channel[channel].get("status") in {"unavailable", "provider_fallback"}
    ]
    if route.get("executed_channels") != executed_channels:
        raise ValueError("routing executed-channel summary does not match channel evidence")
    if route.get("contributing_channels") != contributing_channels:
        raise ValueError("routing contributing-channel summary does not match channel evidence")
    if route.get("degraded_channels") != degraded_channels:
        raise ValueError("routing degraded-channel summary does not match channel evidence")
    if route.get("fusion") != recipe.fusion:
        raise ValueError("routing evidence fusion does not match the recipe")
    expected_fusion_weights = {
        channel: (RRF_WEIGHTS["fts"] if channel == "source_anchor" else RRF_WEIGHTS[channel])
        for channel in recipe.channels
    }
    if route.get("fusion_weights") != expected_fusion_weights:
        raise ValueError("routing evidence fusion weights do not match the frozen policy")
    if route.get("rrf_k") != RRF_K:
        raise ValueError("routing evidence changed the frozen RRF rank constant")
    expected_score_contract = profile.candidate_score_contract if candidate else "legacy_raw_rrf_v1"
    if route.get("score_contract") != expected_score_contract:
        raise ValueError("routing evidence score contract does not match its execution mode")
    expected_anchor_policy = profile.source_anchor_weight_policy if candidate else None
    if route.get("source_anchor_weight_policy") != expected_anchor_policy:
        raise ValueError("routing evidence source-anchor weight policy does not match")
    if route.get("reranker_policy") != recipe.reranker_policy:
        raise ValueError("routing evidence reranker policy does not match the recipe")
    reranker_mode = route.get("reranker_mode")
    if not isinstance(reranker_mode, str) or not reranker_mode:
        raise ValueError("routing evidence omitted actual reranker execution mode")
    if recipe.reranker_policy == "disabled" and reranker_mode not in {
        "disabled",
        "disabled-by-recipe",
    }:
        raise ValueError("routing evidence executed a reranker forbidden by the recipe")
    if recipe.reranker_policy != "disabled" and reranker_mode == "disabled-by-recipe":
        raise ValueError("routing evidence disabled an allowed reranker as if forbidden")
    if route.get("diversity_policy") != recipe.diversity_policy:
        raise ValueError("routing evidence diversity policy does not match the recipe")
    if route.get("candidate_pool_min") != recipe.candidate_pool_min:
        raise ValueError("routing evidence candidate pool minimum does not match the recipe")
    if route.get("candidate_pool_max") != recipe.candidate_pool_max:
        raise ValueError("routing evidence candidate pool maximum does not match the recipe")
    if route.get("rerank_window") != recipe.rerank_window:
        raise ValueError("routing evidence rerank window does not match the recipe")
    if route.get("fallback_recipe_id") != SAFE_RECIPE_ID:
        raise ValueError("routing evidence changed the safe fallback recipe")
    timings = route.get("stage_timings_ms")
    expected_stages = {
        "candidate_retrieval",
        "fusion",
        "governance_scoring",
        "rerank",
        "diversity",
    }
    if not isinstance(timings, dict) or set(timings) != expected_stages:
        raise ValueError("routing evidence stage timing coverage is incomplete")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in timings.values()
    ):
        raise ValueError("routing evidence contains an invalid stage duration")


def _single_memoryos_record(report: dict[str, Any], task_id: str) -> dict[str, Any]:
    records = report.get("records")
    if not isinstance(records, list):
        raise ValueError("routing shadow report is missing records")
    matches = [
        item
        for item in records
        if isinstance(item, dict)
        and item.get("task_id") == task_id
        and item.get("condition") == ExperimentCondition.MEMORYOS.value
    ]
    if len(matches) != 1:
        raise ValueError("routing shadow report requires exactly one MemoryOS task record")
    return matches[0]


def _one_config_hash(record: dict[str, Any]) -> str:
    values = record.get("retrieval_config_hashes")
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], str)
        or not _is_sha256(values[0])
    ):
        raise ValueError("routing shadow record requires exactly one retrieval config hash")
    return values[0]


def _routing_evidence(record: dict[str, Any]) -> list[dict[str, Any]]:
    values = record.get("retrieval_routes")
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, dict) for value in values)
    ):
        raise ValueError("routing shadow record requires routing execution evidence")
    routes = [value for value in values if isinstance(value, dict)]
    run_count = record.get("retrieval_runs")
    raw_indices = [route.get("retrieval_index") for route in routes]
    if isinstance(run_count, bool) or not isinstance(run_count, int) or run_count != len(routes):
        raise ValueError("routing execution evidence does not cover every retrieval run")
    if not all(isinstance(index, int) and not isinstance(index, bool) for index in raw_indices):
        raise ValueError("routing execution evidence has an invalid retrieval index")
    indices = [index for index in raw_indices if isinstance(index, int)]
    if sorted(indices) != list(range(run_count)):
        raise ValueError("routing execution evidence does not cover every retrieval run")
    return routes


def _selected_seed_ids(record: dict[str, Any]) -> tuple[str, ...]:
    values = record.get("selected_seed_ids", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("selected_seed_ids must be a string list")
    return tuple(sorted(set(values)))


def _paired_task_values(
    evaluations: Sequence[RoutingCandidateEvaluation],
) -> tuple[list[float], list[float]]:
    grouped: dict[str, list[RoutingCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        grouped[item.task_id].append(item)
    baseline = [
        sum(float(item.baseline_success) for item in grouped[task_id]) / len(grouped[task_id])
        for task_id in sorted(grouped)
    ]
    candidate = [
        sum(float(item.candidate_success) for item in grouped[task_id]) / len(grouped[task_id])
        for task_id in sorted(grouped)
    ]
    return baseline, candidate


def _slice_success_deltas(
    evaluations: Sequence[RoutingCandidateEvaluation],
    field_name: Literal["repository_id", "agent_model"],
) -> dict[str, float]:
    grouped: dict[str, list[RoutingCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        grouped[str(getattr(item, field_name))].append(item)
    result: dict[str, float] = {}
    for key, items in sorted(grouped.items()):
        baseline, candidate = _paired_task_values(items)
        result[key] = _mean(candidate) - _mean(baseline)
    return result


def _recipe_success_deltas(
    evaluations: Sequence[RoutingCandidateEvaluation],
    recipe_tasks: dict[str, set[str]],
) -> dict[str, float]:
    result: dict[str, float] = {}
    for recipe_id, task_ids in sorted(recipe_tasks.items()):
        items = [
            item
            for item in evaluations
            if item.task_id in task_ids and item.recommended_recipe_counts.get(recipe_id, 0) > 0
        ]
        baseline, candidate = _paired_task_values(items)
        result[recipe_id] = _mean(candidate) - _mean(baseline)
    return result


def _increase_ratio(baseline: list[float], candidate: list[float]) -> float | None:
    if len(baseline) != len(candidate) or not baseline:
        return None
    baseline_mean = _mean(baseline)
    candidate_mean = _mean(candidate)
    if baseline_mean <= 0.0:
        return 0.0 if candidate_mean <= baseline_mean else None
    return (candidate_mean - baseline_mean) / baseline_mean


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _record_protocol_valid(record: dict[str, Any]) -> bool:
    return bool(
        record.get("execution_valid")
        and record.get("memory_usage_valid")
        and record.get("hidden_test_setup_valid")
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("optional numeric value must be a number or null")
    return float(value)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def routing_evaluation_set_digest(
    evaluations: Sequence[RoutingCandidateEvaluation],
) -> str:
    payload = [
        item.model_dump(mode="json")
        for item in sorted(evaluations, key=lambda evaluation: evaluation.evaluation_id)
    ]
    return _canonical_sha256(payload)


__all__ = [
    "RoutingCandidateEvaluation",
    "RoutingPromotionDecision",
    "RoutingPromotionProtocol",
    "evaluate_routing_candidate",
    "routing_evaluation_from_reports",
    "routing_evaluation_set_digest",
]
