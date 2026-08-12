from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memoryos.evaluation.ai_jury import AIJuryPairResult, JuryDecisionStatus
from memoryos.evaluation.executable_ablation import AblationArm, ExecutableAblationRun
from memoryos.evaluation.metrics import bootstrap_mean_difference
from memoryos.evaluation.real_workload_agent import AgentEvidenceType
from memoryos.evaluation.real_workload_models import ExperimentCondition, WorkloadTaskSpec
from memoryos.retrieval_v2.pipeline import retrieval_config_hash
from memoryos.retrieval_v2.scoring import ShadowRetrievalProfile


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalibrationPartition(StrEnum):
    TRAIN = "train"
    DEV = "dev"
    TEST = "test"


class CalibrationLabelTier(StrEnum):
    AI_JURY_PROVISIONAL = "ai_jury_provisional"
    EXECUTABLE_FIXTURE = "executable_outcome_fixture"
    EXECUTABLE_REAL = "executable_outcome_real"


class PairwiseFeatureObservation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1, max_length=160)
    query_id: str = Field(min_length=1, max_length=160)
    repository_id: str = Field(min_length=1, max_length=160)
    partition: CalibrationPartition
    candidate_a_id: str = Field(min_length=1, max_length=160)
    candidate_b_id: str = Field(min_length=1, max_length=160)
    feature_delta: dict[str, float] = Field(min_length=1, max_length=64)
    target_a_probability: float = Field(ge=0.0, le=1.0)
    sample_weight: float = Field(gt=0.0, le=100.0)
    label_tier: CalibrationLabelTier
    execution_agent_model: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("feature_delta")
    @classmethod
    def require_finite_features(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not name or not math.isfinite(number) for name, number in value.items()):
            raise ValueError("feature names must be non-empty and values must be finite")
        return value

    @model_validator(mode="after")
    def validate_observation(self) -> PairwiseFeatureObservation:
        if self.candidate_a_id == self.candidate_b_id:
            raise ValueError("pairwise feature candidates must be distinct")
        executable = self.label_tier in {
            CalibrationLabelTier.EXECUTABLE_FIXTURE,
            CalibrationLabelTier.EXECUTABLE_REAL,
        }
        if executable != (self.execution_agent_model is not None):
            raise ValueError("executable observations must name their execution agent model")
        return self


class CalibrationCandidateFeatureVector(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    query_id: str = Field(min_length=1, max_length=160)
    repository_id: str = Field(min_length=1, max_length=160)
    partition: CalibrationPartition
    candidate_id: str = Field(min_length=1, max_length=160)
    features: dict[str, float] = Field(min_length=1, max_length=64)
    trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("features")
    @classmethod
    def require_finite_features(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not name or not math.isfinite(number) for name, number in value.items()):
            raise ValueError("feature names must be non-empty and values must be finite")
        return value


class WeightTrainingProtocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    feature_names: list[str] = Field(min_length=1, max_length=64)
    non_negative_features: list[str] = Field(default_factory=list, max_length=64)
    non_positive_features: list[str] = Field(default_factory=list, max_length=64)
    hard_gate_features: list[str] = Field(default_factory=list, max_length=64)
    baseline_weights: dict[str, float] = Field(default_factory=dict)
    l2_candidates: list[float] = Field(default=[0.02, 0.08, 0.32], min_length=1, max_length=20)
    learning_rate: float = Field(default=0.25, gt=0.0, le=5.0)
    iterations: int = Field(default=2500, ge=100, le=100_000)
    max_absolute_weight: float = Field(default=20.0, gt=0.0, le=1000.0)
    min_train_repositories: int = Field(default=3, ge=1, le=1000)
    require_repository_holdout: bool = True
    require_development_partition: bool = True
    forbid_test_observations: bool = True
    hyperparameter_selection: Literal["development_weighted_log_loss"] = (
        "development_weighted_log_loss"
    )
    shadow_mmr_lambda: float = Field(default=0.78, ge=0.0, le=1.0)
    shadow_context_utility_mode: Literal["profile_score_per_character"] = (
        "profile_score_per_character"
    )
    structural_parameter_source: Literal["frozen_baseline_subject_to_sealed_promotion"] = (
        "frozen_baseline_subject_to_sealed_promotion"
    )
    required_label_tiers: list[CalibrationLabelTier] = Field(
        default=[
            CalibrationLabelTier.AI_JURY_PROVISIONAL,
            CalibrationLabelTier.EXECUTABLE_REAL,
        ],
        min_length=1,
    )
    allow_fixture_observations: bool = False

    @model_validator(mode="after")
    def validate_features(self) -> WeightTrainingProtocol:
        features = set(self.feature_names)
        if len(features) != len(self.feature_names):
            raise ValueError("feature_names must be unique")
        if len(set(self.non_negative_features)) != len(self.non_negative_features):
            raise ValueError("non_negative_features must be unique")
        if len(set(self.non_positive_features)) != len(self.non_positive_features):
            raise ValueError("non_positive_features must be unique")
        if len(set(self.hard_gate_features)) != len(self.hard_gate_features):
            raise ValueError("hard_gate_features must be unique")
        unknown_non_negative = set(self.non_negative_features) - features
        if unknown_non_negative:
            raise ValueError(f"unknown non-negative features: {sorted(unknown_non_negative)}")
        unknown_non_positive = set(self.non_positive_features) - features
        if unknown_non_positive:
            raise ValueError(f"unknown non-positive features: {sorted(unknown_non_positive)}")
        sign_overlap = set(self.non_negative_features) & set(self.non_positive_features)
        if sign_overlap:
            raise ValueError(f"features cannot have both sign constraints: {sorted(sign_overlap)}")
        leaked_gates = set(self.hard_gate_features) & features
        if leaked_gates:
            raise ValueError(f"hard safety gates cannot be learned weights: {sorted(leaked_gates)}")
        if set(self.baseline_weights) not in (set(), features):
            raise ValueError("baseline_weights must be empty or cover every learned feature")
        if any(not math.isfinite(value) for value in self.baseline_weights.values()):
            raise ValueError("baseline weights must be finite")
        if len(set(self.l2_candidates)) != len(self.l2_candidates) or any(
            not math.isfinite(value) or value <= 0 or value > 10 for value in self.l2_candidates
        ):
            raise ValueError("l2_candidates must be unique finite values in (0, 10]")
        if len(set(self.required_label_tiers)) != len(self.required_label_tiers):
            raise ValueError("required_label_tiers must be unique")
        return self


class PairwiseModelMetrics(StrictModel):
    rows: int = Field(ge=0)
    weighted_log_loss: float | None = Field(default=None, ge=0.0)
    weighted_brier_score: float | None = Field(default=None, ge=0.0)
    decisive_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)


class LearnedWeightProfile(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["candidate_only"] = "candidate_only"
    objective: Literal["regularized_pairwise_logistic"] = "regularized_pairwise_logistic"
    feature_names: list[str]
    weights: dict[str, float]
    feature_scales: dict[str, float]
    selected_l2: float = Field(gt=0.0, le=10.0)
    shadow_mmr_lambda: float = Field(ge=0.0, le=1.0)
    shadow_context_utility_mode: Literal["profile_score_per_character"]
    structural_parameter_source: Literal["frozen_baseline_subject_to_sealed_promotion"]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations_by_partition: dict[str, int]
    label_tier_counts: dict[str, int]
    training_repositories: list[str]
    development_repositories: list[str]
    training_agent_models: list[str]
    metrics: dict[str, PairwiseModelMetrics]
    baseline_metrics: dict[str, PairwiseModelMetrics] | None = None
    candidate_beats_baseline_on_dev: bool | None = None
    production_eligible: Literal[False] = False
    limitations: list[str]


class WeightCandidateEvaluation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=160)
    repository_id: str = Field(min_length=1, max_length=160)
    sequence_id: str = Field(min_length=1, max_length=160)
    repeat_id: str = Field(min_length=1, max_length=160)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_scoring_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_profile_id: str = Field(min_length=1, max_length=160)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_model: str = Field(min_length=1, max_length=300)
    evidence_type: AgentEvidenceType
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


class WeightPromotionProtocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    min_tasks: int = Field(default=50, ge=1, le=100_000)
    min_repositories: int = Field(default=3, ge=1, le=1000)
    min_sequences: int = Field(default=10, ge=1, le=100_000)
    min_agent_models: int = Field(default=1, ge=1, le=100)
    min_tasks_per_agent_model: int = Field(default=10, ge=1, le=100_000)
    success_ci95_low_must_exceed: float = Field(default=0.0, ge=-1.0, le=1.0)
    max_latency_increase_ratio: float = Field(default=0.25, ge=0.0, le=10.0)
    max_cost_increase_ratio: float = Field(default=0.25, ge=0.0, le=10.0)
    require_unseen_agent_models: bool = True
    require_nonnegative_worst_repository_delta: bool = True
    require_nonnegative_worst_agent_delta: bool = True
    require_complete_agent_task_matrix: bool = True
    reject_any_task_safety_regression: bool = True
    require_complete_cost_accounting: bool = True
    expected_training_protocol_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class WeightPromotionDecision(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool
    status: Literal["approved_for_atomic_activation", "rejected"]
    reasons: list[str]
    tasks: int = Field(ge=0)
    repositories: int = Field(ge=0)
    sequences: int = Field(ge=0)
    agent_models: int = Field(ge=0)
    success_effect: dict[str, float] | None
    safety_effect: dict[str, float] | None
    latency_effect: dict[str, float] | None
    cost_effect: dict[str, float] | None
    worst_repository_success_delta: float | None
    worst_agent_success_delta: float | None
    activates_automatically: Literal[False] = False


def observation_from_ai_jury(
    result: AIJuryPairResult,
    *,
    observation_id: str,
    repository_id: str,
    partition: CalibrationPartition,
    feature_delta: dict[str, float],
) -> PairwiseFeatureObservation:
    if result.status is JuryDecisionStatus.INVALID or result.training_weight <= 0:
        raise ValueError("invalid AI-jury results cannot become training observations")
    return PairwiseFeatureObservation(
        id=observation_id,
        query_id=result.query_id,
        repository_id=repository_id,
        partition=partition,
        candidate_a_id=result.candidate_a_id,
        candidate_b_id=result.candidate_b_id,
        feature_delta=feature_delta,
        target_a_probability=result.probability_a + 0.5 * result.probability_tie,
        sample_weight=result.training_weight,
        label_tier=CalibrationLabelTier.AI_JURY_PROVISIONAL,
    )


def observations_from_ai_jury(
    results: Sequence[AIJuryPairResult],
    candidates: Sequence[CalibrationCandidateFeatureVector],
) -> list[PairwiseFeatureObservation]:
    indexed: dict[tuple[str, str], CalibrationCandidateFeatureVector] = {}
    for candidate in candidates:
        key = (candidate.query_id, candidate.candidate_id)
        if key in indexed:
            raise ValueError(f"duplicate candidate feature vector: {key}")
        indexed[key] = candidate
    observations: list[PairwiseFeatureObservation] = []
    for result in results:
        if result.status is JuryDecisionStatus.INVALID or result.training_weight <= 0:
            continue
        try:
            left = indexed[(result.query_id, result.candidate_a_id)]
            right = indexed[(result.query_id, result.candidate_b_id)]
        except KeyError as exc:
            raise ValueError(
                f"AI-jury result is missing candidate features: {exc.args[0]}"
            ) from exc
        if left.repository_id != right.repository_id or left.partition is not right.partition:
            raise ValueError("an AI-jury pair crosses repository or partition boundaries")
        if set(left.features) != set(right.features):
            raise ValueError("an AI-jury pair uses incompatible feature schemas")
        delta = {
            feature: left.features[feature] - right.features[feature] for feature in left.features
        }
        identity = hashlib.sha256(
            "\x1f".join(
                [
                    result.comparison_id,
                    left.trace_sha256,
                    right.trace_sha256,
                ]
            ).encode()
        ).hexdigest()[:32]
        observations.append(
            observation_from_ai_jury(
                result,
                observation_id=f"jury-{identity}",
                repository_id=left.repository_id,
                partition=left.partition,
                feature_delta=delta,
            )
        )
    return observations


def observation_from_ablation_pair(
    full: ExecutableAblationRun,
    minus: ExecutableAblationRun,
    *,
    observation_id: str,
    partition: CalibrationPartition,
    feature_delta: dict[str, float] | None = None,
) -> PairwiseFeatureObservation | None:
    if full.arm is not AblationArm.MEMORYOS_FULL:
        raise ValueError("the full side of an ablation observation must use memoryos_full")
    if minus.arm is not AblationArm.MEMORYOS_MINUS_MEMORY:
        raise ValueError("the minus side must use memoryos_minus_memory")
    if (
        full.task_id != minus.task_id
        or full.repository_id != minus.repository_id
        or full.base_commit != minus.base_commit
        or full.prompt_sha256 != minus.prompt_sha256
        or full.runtime_sha256 != minus.runtime_sha256
        or full.agent_model != minus.agent_model
        or full.repeat_id != minus.repeat_id
        or full.evidence_type is not minus.evidence_type
    ):
        raise ValueError("ablation training pair changed a controlled dimension")
    memory_id = minus.excluded_memory_id
    assert memory_id is not None
    if (
        not full.protocol_valid
        or not minus.protocol_valid
        or memory_id not in full.selected_memory_ids
    ):
        return None
    if full.functional_success == minus.functional_success:
        return None
    if feature_delta is None:
        try:
            candidate_trace = full.candidate_traces[memory_id]
        except KeyError as exc:
            raise ValueError("full ablation run is missing the selected memory trace") from exc
        from memoryos.evaluation.retrieval_calibration_features import (
            extract_calibratable_features,
        )

        feature_delta = extract_calibratable_features(candidate_trace)
    label_tier = (
        CalibrationLabelTier.EXECUTABLE_REAL
        if full.evidence_type is AgentEvidenceType.REAL_CODING_AGENT
        else CalibrationLabelTier.EXECUTABLE_FIXTURE
    )
    control_id = "without-" + hashlib.sha256(memory_id.encode()).hexdigest()[:24]
    return PairwiseFeatureObservation(
        id=observation_id,
        query_id=full.task_id,
        repository_id=full.repository_id,
        partition=partition,
        candidate_a_id=memory_id,
        candidate_b_id=control_id,
        feature_delta=feature_delta,
        target_a_probability=1.0 if full.functional_success else 0.0,
        sample_weight=1.0,
        label_tier=label_tier,
        execution_agent_model=full.agent_model,
    )


def train_candidate_weights(
    observations: Sequence[PairwiseFeatureObservation],
    protocol: WeightTrainingProtocol,
) -> LearnedWeightProfile:
    if not observations:
        raise ValueError("weight training requires observations")
    if len({observation.id for observation in observations}) != len(observations):
        raise ValueError("weight training observation IDs must be unique")
    feature_names = protocol.feature_names
    expected_features = set(feature_names)
    for observation in observations:
        if set(observation.feature_delta) != expected_features:
            raise ValueError(f"observation {observation.id} does not cover the protocol features")
    train = [
        observation
        for observation in observations
        if observation.partition is CalibrationPartition.TRAIN
    ]
    if not train:
        raise ValueError("weight training requires a train partition")
    train_repositories = sorted({observation.repository_id for observation in train})
    if len(train_repositories) < protocol.min_train_repositories:
        raise ValueError("weight training has too few train repositories")
    if protocol.require_repository_holdout:
        _validate_repository_holdout(observations)
    if protocol.forbid_test_observations and any(
        item.partition is CalibrationPartition.TEST for item in observations
    ):
        raise ValueError("weight training must not inspect sealed test observations")
    if not protocol.allow_fixture_observations and any(
        item.label_tier is CalibrationLabelTier.EXECUTABLE_FIXTURE for item in observations
    ):
        raise ValueError("fixture observations validate plumbing and cannot train this profile")
    training_tiers = {item.label_tier for item in train}
    missing_tiers = set(protocol.required_label_tiers) - training_tiers
    if missing_tiers:
        raise ValueError(
            "weight training partition is missing required label tiers: "
            + ", ".join(sorted(item.value for item in missing_tiers))
        )
    if protocol.require_development_partition and not any(
        item.partition is CalibrationPartition.DEV for item in observations
    ):
        raise ValueError("weight training requires a development partition")

    train_matrix = _matrix(train, feature_names)
    feature_scales_array = np.sqrt(np.mean(np.square(train_matrix), axis=0))
    feature_scales_array = np.where(feature_scales_array < 1e-9, 1.0, feature_scales_array)
    non_negative_indices = {
        feature_names.index(feature) for feature in protocol.non_negative_features
    }
    non_positive_indices = {
        feature_names.index(feature) for feature in protocol.non_positive_features
    }
    candidates = [
        (
            l2,
            _fit_pairwise_weights(
                train,
                feature_names=feature_names,
                feature_scales=feature_scales_array,
                non_negative_indices=non_negative_indices,
                non_positive_indices=non_positive_indices,
                l2=l2,
                learning_rate=protocol.learning_rate,
                iterations=protocol.iterations,
                max_absolute_weight=protocol.max_absolute_weight,
            ),
        )
        for l2 in sorted(protocol.l2_candidates)
    ]
    development = [item for item in observations if item.partition is CalibrationPartition.DEV]
    if not development:
        raise ValueError("development observations are required to select regularization")
    scored_candidates = [
        (
            _required_metric(
                _metrics_for(development, feature_names, candidate_weights).weighted_log_loss
            ),
            l2,
            candidate_weights,
        )
        for l2, candidate_weights in candidates
    ]
    _, selected_l2, original_scale_weights = min(
        scored_candidates,
        key=lambda item: (item[0], item[1]),
    )
    weights = {
        feature: round(float(original_scale_weights[index]), 12)
        for index, feature in enumerate(feature_names)
    }
    scales = {
        feature: round(float(feature_scales_array[index]), 12)
        for index, feature in enumerate(feature_names)
    }
    metrics = {
        partition.value: _metrics_for(
            [item for item in observations if item.partition is partition],
            feature_names,
            original_scale_weights,
        )
        for partition in CalibrationPartition
    }
    baseline_metrics: dict[str, PairwiseModelMetrics] | None = None
    beats_baseline: bool | None = None
    if protocol.baseline_weights:
        baseline = np.asarray(
            [protocol.baseline_weights[feature] for feature in feature_names],
            dtype=np.float64,
        )
        baseline_metrics = {
            partition.value: _metrics_for(
                [item for item in observations if item.partition is partition],
                feature_names,
                baseline,
            )
            for partition in CalibrationPartition
        }
        candidate_dev = metrics[CalibrationPartition.DEV.value].weighted_log_loss
        baseline_dev = baseline_metrics[CalibrationPartition.DEV.value].weighted_log_loss
        beats_baseline = (
            None if candidate_dev is None or baseline_dev is None else candidate_dev < baseline_dev
        )
    protocol_hash = weight_training_protocol_digest(protocol)
    observations_hash = _canonical_hash(
        [
            item.model_dump(mode="json")
            for item in sorted(observations, key=lambda observation: observation.id)
        ]
    )
    payload = {
        "objective": "regularized_pairwise_logistic",
        "feature_names": feature_names,
        "weights": weights,
        "feature_scales": scales,
        "selected_l2": selected_l2,
        "shadow_mmr_lambda": protocol.shadow_mmr_lambda,
        "shadow_context_utility_mode": protocol.shadow_context_utility_mode,
        "structural_parameter_source": protocol.structural_parameter_source,
        "protocol_sha256": protocol_hash,
        "observations_sha256": observations_hash,
        "observations_by_partition": dict(
            sorted(Counter(item.partition.value for item in observations).items())
        ),
        "label_tier_counts": dict(
            sorted(Counter(item.label_tier.value for item in observations).items())
        ),
        "training_repositories": train_repositories,
        "development_repositories": sorted(
            {
                item.repository_id
                for item in observations
                if item.partition is CalibrationPartition.DEV
            }
        ),
        "training_agent_models": sorted(
            {item.execution_agent_model for item in train if item.execution_agent_model is not None}
        ),
        "metrics": {
            partition: value.model_dump(mode="json") for partition, value in metrics.items()
        },
        "baseline_metrics": (
            None
            if baseline_metrics is None
            else {
                partition: value.model_dump(mode="json")
                for partition, value in baseline_metrics.items()
            }
        ),
        "candidate_beats_baseline_on_dev": beats_baseline,
    }
    profile_hash = _canonical_hash(payload)
    return LearnedWeightProfile(
        feature_names=feature_names,
        weights=weights,
        feature_scales=scales,
        selected_l2=selected_l2,
        shadow_mmr_lambda=protocol.shadow_mmr_lambda,
        shadow_context_utility_mode=protocol.shadow_context_utility_mode,
        structural_parameter_source=protocol.structural_parameter_source,
        protocol_sha256=protocol_hash,
        observations_sha256=observations_hash,
        profile_sha256=profile_hash,
        observations_by_partition=payload["observations_by_partition"],
        label_tier_counts=payload["label_tier_counts"],
        training_repositories=train_repositories,
        development_repositories=payload["development_repositories"],
        training_agent_models=payload["training_agent_models"],
        metrics=metrics,
        baseline_metrics=baseline_metrics,
        candidate_beats_baseline_on_dev=beats_baseline,
        limitations=[
            "AI-jury observations are weak supervision and are downweighted inputs, not truth.",
            "Executable fixture observations validate plumbing only.",
            "The profile remains candidate-only until a sealed executable promotion gate passes.",
            "Hard safety gates are deliberately excluded from learned features.",
            "L2 regularization is selected on the repository-held-out development partition.",
            "MMR and context cost structure remain frozen baseline parameters until "
            "sealed promotion.",
        ],
    )


def weight_evaluation_from_reports(
    profile: LearnedWeightProfile,
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
    task: WorkloadTaskSpec,
    *,
    repeat_id: str,
    sealed: bool,
) -> WeightCandidateEvaluation:
    shadow_profile = shadow_scoring_profile_from_learned(profile)
    shadow_digest = shadow_profile.digest()
    baseline = _single_memoryos_record(baseline_report, task.id)
    candidate = _single_memoryos_record(candidate_report, task.id)
    baseline_manifest = baseline_report.get("manifest")
    if not isinstance(baseline_manifest, dict) or baseline_manifest != candidate_report.get(
        "manifest"
    ):
        raise ValueError("weight shadow pair changed or omitted the task manifest")
    if (
        baseline.get("repository_id") != task.repository_id
        or candidate.get("repository_id") != task.repository_id
        or baseline.get("sequence_id") != task.sequence_id
        or candidate.get("sequence_id") != task.sequence_id
    ):
        raise ValueError("weight shadow records do not match the registered task")
    if baseline_report.get("run_id") == candidate_report.get("run_id"):
        raise ValueError("baseline and candidate reports must come from distinct runs")
    if baseline.get("prompt_sha256") != candidate.get("prompt_sha256"):
        raise ValueError("weight shadow pair changed the task prompt")
    runtime_sha256 = str(baseline_report.get("runtime_spec_sha256", ""))
    if (
        not _is_sha256(runtime_sha256)
        or candidate_report.get("runtime_spec_sha256") != runtime_sha256
    ):
        raise ValueError("weight shadow pair changed or omitted the pinned agent runtime")
    baseline_runtime = baseline_report.get("runtime")
    candidate_runtime = candidate_report.get("runtime")
    if not isinstance(baseline_runtime, dict) or baseline_runtime != candidate_runtime:
        raise ValueError("weight shadow pair changed runtime metadata")
    if baseline_report.get("scoring_profile_sha256") is not None:
        raise ValueError("weight shadow baseline unexpectedly used a scoring profile")
    if baseline.get("scoring_profile_sha256") is not None:
        raise ValueError("weight shadow baseline record unexpectedly used a scoring profile")
    if candidate_report.get("scoring_profile_sha256") != shadow_digest:
        raise ValueError("candidate report did not bind the expected shadow profile")
    if candidate.get("scoring_profile_sha256") != shadow_digest:
        raise ValueError("candidate record did not bind the expected shadow profile")
    baseline_configs = _one_config_hash(baseline)
    candidate_configs = _one_config_hash(candidate)
    if baseline_configs != retrieval_config_hash():
        raise ValueError("baseline report did not execute the frozen production config")
    if candidate_configs != retrieval_config_hash(shadow_profile):
        raise ValueError("candidate report did not execute the expected shadow config")
    if sealed:
        known_repositories = set(profile.training_repositories) | set(
            profile.development_repositories
        )
        if (
            baseline_manifest.get("tier") != "public_replay"
            or baseline_runtime.get("evidence_type") != AgentEvidenceType.REAL_CODING_AGENT.value
            or task.repository_id in known_repositories
            or task.solution_commit is None
            or task.source_published_at is None
            or task.hidden_test.hidden_patch_sha256 is None
        ):
            raise ValueError("the requested sealed weight shadow pair lacks sealed provenance")
    prompt_sha256 = str(baseline["prompt_sha256"])
    identity = hashlib.sha256(
        "\x1f".join(
            [
                task.id,
                str(baseline_runtime.get("model", "")),
                repeat_id,
                prompt_sha256,
                profile.profile_sha256,
            ]
        ).encode()
    ).hexdigest()[:40]
    protocol_valid = _record_protocol_valid(baseline) and _record_protocol_valid(candidate)
    return WeightCandidateEvaluation(
        evaluation_id=f"weight-shadow-{identity}",
        task_id=task.id,
        repository_id=task.repository_id,
        sequence_id=task.sequence_id,
        repeat_id=repeat_id,
        base_commit=task.base_commit,
        candidate_profile_sha256=profile.profile_sha256,
        candidate_scoring_profile_sha256=shadow_digest,
        baseline_profile_id=f"retrieval-config-{baseline_configs}",
        prompt_sha256=prompt_sha256,
        runtime_sha256=runtime_sha256,
        agent_model=str(baseline_runtime.get("model", "")),
        evidence_type=AgentEvidenceType(str(baseline_runtime.get("evidence_type", ""))),
        sealed=sealed,
        protocol_valid=protocol_valid,
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
    )


def evaluate_weight_candidate(
    profile: LearnedWeightProfile,
    evaluations: Sequence[WeightCandidateEvaluation],
    *,
    protocol: WeightPromotionProtocol | None = None,
    bootstrap_seed: int = 20260812,
) -> WeightPromotionDecision:
    configured = protocol or WeightPromotionProtocol()
    if _canonical_hash(_profile_hash_payload(profile)) != profile.profile_sha256:
        raise ValueError("candidate profile content does not match profile_sha256")
    shadow_profile = shadow_scoring_profile_from_learned(profile)
    reasons: list[str] = []
    if profile.label_tier_counts.get(CalibrationLabelTier.EXECUTABLE_FIXTURE.value, 0) > 0:
        reasons.append("promotion rejects profiles trained with deterministic fixture labels")
    if len({item.evaluation_id for item in evaluations}) != len(evaluations):
        reasons.append("evaluation IDs must be unique")
    comparison_keys = {(item.task_id, item.agent_model, item.repeat_id) for item in evaluations}
    if len(comparison_keys) != len(evaluations):
        reasons.append("task/agent/repeat promotion comparisons must be unique")
    tasks = sorted({item.task_id for item in evaluations})
    repositories = sorted({item.repository_id for item in evaluations})
    sequences = sorted({item.sequence_id for item in evaluations})
    agent_models = sorted({item.agent_model for item in evaluations})
    if any(item.candidate_profile_sha256 != profile.profile_sha256 for item in evaluations):
        reasons.append("every evaluation must bind to the candidate profile hash")
    if any(
        item.candidate_scoring_profile_sha256 != shadow_profile.digest() for item in evaluations
    ):
        reasons.append("every evaluation must bind to the shadow scoring projection")
    baseline_profiles = {item.baseline_profile_id for item in evaluations}
    if len(baseline_profiles) != 1:
        reasons.append("every evaluation must use the same frozen baseline profile")
    runtimes_by_agent: dict[str, set[str]] = defaultdict(set)
    for item in evaluations:
        runtimes_by_agent[item.agent_model].add(item.runtime_sha256)
    if any(len(runtime_hashes) != 1 for runtime_hashes in runtimes_by_agent.values()):
        reasons.append("each promotion agent model must use one pinned runtime")
    if (
        configured.expected_training_protocol_sha256 is not None
        and profile.protocol_sha256 != configured.expected_training_protocol_sha256
    ):
        reasons.append("candidate profile was trained under a different frozen protocol")
    if len(tasks) < configured.min_tasks:
        reasons.append(f"requires at least {configured.min_tasks} sealed tasks")
    if len(repositories) < configured.min_repositories:
        reasons.append(f"requires at least {configured.min_repositories} repositories")
    if len(sequences) < configured.min_sequences:
        reasons.append(f"requires at least {configured.min_sequences} task sequences")
    if len(agent_models) < configured.min_agent_models:
        reasons.append(f"requires at least {configured.min_agent_models} agent models")
    task_counts_by_agent = {
        agent_model: len({item.task_id for item in evaluations if item.agent_model == agent_model})
        for agent_model in agent_models
    }
    if any(count < configured.min_tasks_per_agent_model for count in task_counts_by_agent.values()):
        reasons.append(
            f"each agent model requires at least {configured.min_tasks_per_agent_model} tasks"
        )
    if configured.require_complete_agent_task_matrix:
        expected_agents = set(agent_models)
        agents_by_task = {
            task_id: {item.agent_model for item in evaluations if item.task_id == task_id}
            for task_id in tasks
        }
        if any(agents != expected_agents for agents in agents_by_task.values()):
            reasons.append("every sealed task must run on every promotion agent model")
        repeats_by_task_agent = {
            (task_id, agent_model): {
                item.repeat_id
                for item in evaluations
                if item.task_id == task_id and item.agent_model == agent_model
            }
            for task_id in tasks
            for agent_model in agent_models
        }
        if any(
            len(
                {
                    frozenset(repeats_by_task_agent[(task_id, agent_model)])
                    for agent_model in agent_models
                }
            )
            != 1
            for task_id in tasks
        ):
            reasons.append("every agent model must use the same repeat IDs within each sealed task")
    if any(not item.sealed for item in evaluations):
        reasons.append("every promotion task must be sealed")
    if any(not item.protocol_valid for item in evaluations):
        reasons.append("every promotion task must pass protocol validation")
    if any(item.evidence_type is not AgentEvidenceType.REAL_CODING_AGENT for item in evaluations):
        reasons.append("promotion accepts real_coding_agent evidence only")
    if configured.require_unseen_agent_models and set(agent_models) & set(
        profile.training_agent_models
    ):
        reasons.append("promotion agent models must be absent from weight-training observations")
    observed_repositories = set(profile.training_repositories) | set(
        profile.development_repositories
    )
    if set(repositories) & observed_repositories:
        reasons.append("promotion repositories must be absent from train and development")

    success_values = _task_level_values(
        evaluations,
        baseline=lambda item: float(item.baseline_success),
        candidate=lambda item: float(item.candidate_success),
    )
    success = _evaluation_estimate(*success_values, seed=bootstrap_seed)
    safety_values = _task_level_values(
        evaluations,
        baseline=lambda item: float(item.baseline_safety_violations),
        candidate=lambda item: float(item.candidate_safety_violations),
    )
    safety = _evaluation_estimate(*safety_values, seed=bootstrap_seed + 1)
    latency_values = _task_level_values(
        evaluations,
        baseline=lambda item: item.baseline_latency_seconds,
        candidate=lambda item: item.candidate_latency_seconds,
    )
    latency = _evaluation_estimate(*latency_values, seed=bootstrap_seed + 2)
    cost_pairs = [
        item
        for item in evaluations
        if item.baseline_cost_usd is not None and item.candidate_cost_usd is not None
    ]
    if configured.require_complete_cost_accounting and len(cost_pairs) != len(evaluations):
        reasons.append("promotion requires complete paired cost accounting")
    cost_values = _task_level_values(
        cost_pairs,
        baseline=lambda item: _required_float(item.baseline_cost_usd),
        candidate=lambda item: _required_float(item.candidate_cost_usd),
    )
    cost = _evaluation_estimate(*cost_values, seed=bootstrap_seed + 3)
    if success is None or success["ci95_low"] <= configured.success_ci95_low_must_exceed:
        reasons.append("success improvement lacks a strictly positive paired 95% lower bound")
    if safety is None or safety["ci95_high"] > 0:
        reasons.append("candidate safety violations are not demonstrably non-increasing")
    if configured.reject_any_task_safety_regression and any(
        item.candidate_safety_violations > item.baseline_safety_violations for item in evaluations
    ):
        reasons.append("at least one sealed task has a safety regression")
    baseline_latency = _mean(latency_values[0])
    candidate_latency = _mean(latency_values[1])
    if baseline_latency == 0.0:
        if candidate_latency > 0.0:
            reasons.append("candidate adds latency to a zero-latency baseline")
    elif candidate_latency > baseline_latency * (1.0 + configured.max_latency_increase_ratio):
        reasons.append("candidate exceeds the latency increase limit")
    if cost_pairs:
        baseline_cost = _mean(cost_values[0])
        candidate_cost = _mean(cost_values[1])
        if baseline_cost == 0.0:
            if candidate_cost > 0.0:
                reasons.append("candidate adds cost to a zero-cost baseline")
        elif candidate_cost > baseline_cost * (1.0 + configured.max_cost_increase_ratio):
            reasons.append("candidate exceeds the cost increase limit")
    by_repository: dict[str, list[WeightCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        by_repository[item.repository_id].append(item)
    repository_deltas = {
        repository: _mean([float(item.candidate_success) for item in items])
        - _mean([float(item.baseline_success) for item in items])
        for repository, items in by_repository.items()
    }
    worst_repository = min(repository_deltas.values()) if repository_deltas else None
    if (
        configured.require_nonnegative_worst_repository_delta
        and worst_repository is not None
        and worst_repository < 0
    ):
        reasons.append("at least one held-out repository regresses")
    by_agent: dict[str, list[WeightCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        by_agent[item.agent_model].append(item)
    agent_deltas = {
        agent_model: _mean([float(item.candidate_success) for item in items])
        - _mean([float(item.baseline_success) for item in items])
        for agent_model, items in by_agent.items()
    }
    worst_agent = min(agent_deltas.values()) if agent_deltas else None
    if (
        configured.require_nonnegative_worst_agent_delta
        and worst_agent is not None
        and worst_agent < 0
    ):
        reasons.append("at least one held-out agent model regresses")
    approved = not reasons
    return WeightPromotionDecision(
        profile_sha256=profile.profile_sha256,
        approved=approved,
        status="approved_for_atomic_activation" if approved else "rejected",
        reasons=sorted(set(reasons)),
        tasks=len(tasks),
        repositories=len(repositories),
        sequences=len(sequences),
        agent_models=len(agent_models),
        success_effect=success,
        safety_effect=safety,
        latency_effect=latency,
        cost_effect=cost,
        worst_repository_success_delta=worst_repository,
        worst_agent_success_delta=worst_agent,
    )


def _validate_repository_holdout(
    observations: Sequence[PairwiseFeatureObservation],
) -> None:
    repositories_by_partition = {
        partition: {item.repository_id for item in observations if item.partition is partition}
        for partition in CalibrationPartition
    }
    for left_index, left in enumerate(CalibrationPartition):
        for right in list(CalibrationPartition)[left_index + 1 :]:
            overlap = repositories_by_partition[left] & repositories_by_partition[right]
            if overlap:
                raise ValueError(
                    f"repository holdout violated by {left.value}/{right.value}: {sorted(overlap)}"
                )


def _matrix(
    observations: Sequence[PairwiseFeatureObservation],
    feature_names: list[str],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    return np.asarray(
        [
            [observation.feature_delta[feature] for feature in feature_names]
            for observation in observations
        ],
        dtype=np.float64,
    )


def _fit_pairwise_weights(
    train: Sequence[PairwiseFeatureObservation],
    *,
    feature_names: list[str],
    feature_scales: np.ndarray[Any, np.dtype[np.float64]],
    non_negative_indices: set[int],
    non_positive_indices: set[int],
    l2: float,
    learning_rate: float,
    iterations: int,
    max_absolute_weight: float,
) -> np.ndarray[Any, np.dtype[np.float64]]:
    scaled_train = _matrix(train, feature_names) / feature_scales
    targets = np.asarray([item.target_a_probability for item in train], dtype=np.float64)
    sample_weights = np.asarray([item.sample_weight for item in train], dtype=np.float64)
    learned = np.zeros(len(feature_names), dtype=np.float64)
    denominator = max(float(sample_weights.sum()), 1.0)
    for iteration in range(iterations):
        predictions = _sigmoid_array(scaled_train @ learned)
        gradient = (
            scaled_train.T @ ((predictions - targets) * sample_weights) / denominator + l2 * learned
        )
        step = learning_rate / math.sqrt(1.0 + iteration / 250.0)
        learned -= step * gradient
        learned = np.clip(learned, -max_absolute_weight, max_absolute_weight)
        for index in non_negative_indices:
            learned[index] = max(0.0, learned[index])
        for index in non_positive_indices:
            learned[index] = min(0.0, learned[index])
    return learned / feature_scales


def _metrics_for(
    observations: list[PairwiseFeatureObservation],
    feature_names: list[str],
    weights: np.ndarray[Any, np.dtype[np.float64]],
) -> PairwiseModelMetrics:
    if not observations:
        return PairwiseModelMetrics(rows=0)
    matrix = _matrix(observations, feature_names)
    targets = np.asarray([item.target_a_probability for item in observations], dtype=np.float64)
    importance = np.asarray([item.sample_weight for item in observations], dtype=np.float64)
    predictions = _sigmoid_array(matrix @ weights)
    epsilon = 1e-12
    clipped = np.clip(predictions, epsilon, 1.0 - epsilon)
    total_weight = float(importance.sum())
    log_loss = -float(
        np.sum(importance * (targets * np.log(clipped) + (1.0 - targets) * np.log(1.0 - clipped)))
        / total_weight
    )
    brier = float(np.sum(importance * np.square(predictions - targets)) / total_weight)
    decisive = [index for index, target in enumerate(targets) if not math.isclose(target, 0.5)]
    accuracy = (
        None
        if not decisive
        else sum((predictions[index] > 0.5) == (targets[index] > 0.5) for index in decisive)
        / len(decisive)
    )
    return PairwiseModelMetrics(
        rows=len(observations),
        weighted_log_loss=log_loss,
        weighted_brier_score=brier,
        decisive_accuracy=accuracy,
    )


def _required_metric(value: float | None) -> float:
    if value is None:  # pragma: no cover - development partition is required
        raise ValueError("required development metric is missing")
    return value


def _sigmoid_array(
    values: np.ndarray[Any, np.dtype[np.float64]],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    positive = values >= 0
    result = np.empty_like(values)
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    result[~positive] = exponent / (1.0 + exponent)
    return result


def _evaluation_estimate(
    baseline: list[float],
    candidate: list[float],
    *,
    seed: int,
) -> dict[str, float] | None:
    if not baseline:
        return None
    return bootstrap_mean_difference(baseline, candidate, seed=seed)


def _task_level_values(
    evaluations: Sequence[WeightCandidateEvaluation],
    *,
    baseline: Callable[[WeightCandidateEvaluation], float],
    candidate: Callable[[WeightCandidateEvaluation], float],
) -> tuple[list[float], list[float]]:
    grouped: dict[str, list[WeightCandidateEvaluation]] = defaultdict(list)
    for item in evaluations:
        grouped[item.task_id].append(item)
    return (
        [_mean([baseline(item) for item in grouped[task_id]]) for task_id in sorted(grouped)],
        [_mean([candidate(item) for item in grouped[task_id]]) for task_id in sorted(grouped)],
    )


def _required_float(value: float | None) -> float:
    if value is None:  # pragma: no cover - caller filters missing costs
        raise ValueError("required numeric value is missing")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("optional numeric value must be a number or null")
    return float(value)


def _single_memoryos_record(report: dict[str, Any], task_id: str) -> dict[str, Any]:
    records = report.get("records")
    if not isinstance(records, list):
        raise ValueError("weight shadow report is missing records")
    matches = [
        item
        for item in records
        if isinstance(item, dict)
        and item.get("task_id") == task_id
        and item.get("condition") == ExperimentCondition.MEMORYOS.value
    ]
    if len(matches) != 1:
        raise ValueError("weight shadow report requires exactly one MemoryOS task record")
    return matches[0]


def _one_config_hash(record: dict[str, Any]) -> str:
    values = record.get("retrieval_config_hashes")
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], str)
        or not _is_sha256(values[0])
    ):
        raise ValueError("weight shadow record requires exactly one retrieval config hash")
    return values[0]


def _record_protocol_valid(record: dict[str, Any]) -> bool:
    return bool(
        record.get("execution_valid")
        and record.get("memory_usage_valid")
        and record.get("hidden_test_setup_valid")
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def weight_training_protocol_digest(protocol: WeightTrainingProtocol) -> str:
    return _canonical_hash(protocol.model_dump(mode="json"))


def shadow_scoring_profile_from_learned(
    profile: LearnedWeightProfile,
) -> ShadowRetrievalProfile:
    if _canonical_hash(_profile_hash_payload(profile)) != profile.profile_sha256:
        raise ValueError("candidate profile content does not match profile_sha256")
    return ShadowRetrievalProfile(
        source_profile_sha256=profile.profile_sha256,
        training_protocol_sha256=profile.protocol_sha256,
        weights=profile.weights,
        mmr_lambda=profile.shadow_mmr_lambda,
        context_utility_mode=profile.shadow_context_utility_mode,
        structural_parameter_source=profile.structural_parameter_source,
    )


def _profile_hash_payload(profile: LearnedWeightProfile) -> dict[str, Any]:
    return {
        "objective": profile.objective,
        "feature_names": profile.feature_names,
        "weights": profile.weights,
        "feature_scales": profile.feature_scales,
        "selected_l2": profile.selected_l2,
        "shadow_mmr_lambda": profile.shadow_mmr_lambda,
        "shadow_context_utility_mode": profile.shadow_context_utility_mode,
        "structural_parameter_source": profile.structural_parameter_source,
        "protocol_sha256": profile.protocol_sha256,
        "observations_sha256": profile.observations_sha256,
        "observations_by_partition": profile.observations_by_partition,
        "label_tier_counts": profile.label_tier_counts,
        "training_repositories": profile.training_repositories,
        "development_repositories": profile.development_repositories,
        "training_agent_models": profile.training_agent_models,
        "metrics": {
            partition: value.model_dump(mode="json") for partition, value in profile.metrics.items()
        },
        "baseline_metrics": (
            None
            if profile.baseline_metrics is None
            else {
                partition: value.model_dump(mode="json")
                for partition, value in profile.baseline_metrics.items()
            }
        ),
        "candidate_beats_baseline_on_dev": profile.candidate_beats_baseline_on_dev,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


__all__ = [
    "CalibrationCandidateFeatureVector",
    "CalibrationLabelTier",
    "CalibrationPartition",
    "LearnedWeightProfile",
    "PairwiseFeatureObservation",
    "PairwiseModelMetrics",
    "WeightCandidateEvaluation",
    "WeightPromotionDecision",
    "WeightPromotionProtocol",
    "WeightTrainingProtocol",
    "evaluate_weight_candidate",
    "observation_from_ablation_pair",
    "observation_from_ai_jury",
    "observations_from_ai_jury",
    "shadow_scoring_profile_from_learned",
    "train_candidate_weights",
    "weight_evaluation_from_reports",
    "weight_training_protocol_digest",
]
