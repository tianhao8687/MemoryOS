from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from memoryos.evaluation.ai_jury import (
    AIJuryPairResult,
    JuryDecisionStatus,
    PairwisePreference,
)
from memoryos.evaluation.real_workload_agent import AgentEvidenceType
from memoryos.evaluation.real_workload_models import WorkloadTaskSpec
from memoryos.evaluation.retrieval_calibration_features import (
    CALIBRATABLE_FEATURES,
    default_weight_training_protocol,
)
from memoryos.evaluation.retrieval_weight_calibration import (
    CalibrationCandidateFeatureVector,
    CalibrationLabelTier,
    CalibrationPartition,
    LearnedWeightProfile,
    PairwiseFeatureObservation,
    WeightCandidateEvaluation,
    WeightPromotionProtocol,
    WeightTrainingProtocol,
    evaluate_weight_candidate,
    observation_from_ai_jury,
    observations_from_ai_jury,
    shadow_scoring_profile_from_learned,
    train_candidate_weights,
    weight_evaluation_from_reports,
)
from memoryos.retrieval_v2.pipeline import retrieval_config_hash


def _observations() -> list[PairwiseFeatureObservation]:
    rows: list[PairwiseFeatureObservation] = []
    partitions = [
        (CalibrationPartition.TRAIN, "train-a", 10),
        (CalibrationPartition.TRAIN, "train-b", 10),
        (CalibrationPartition.TRAIN, "train-c", 10),
        (CalibrationPartition.DEV, "dev", 8),
    ]
    index = 0
    for partition, repository, count in partitions:
        for local_index in range(count):
            prefers_a = local_index % 2 == 0
            signal = 1.0 if prefers_a else -1.0
            noise = 1.0 if local_index % 3 == 0 else -1.0
            rows.append(
                PairwiseFeatureObservation(
                    id=f"observation-{index:03d}",
                    query_id=f"query-{index:03d}",
                    repository_id=repository,
                    partition=partition,
                    candidate_a_id=f"candidate-a-{index:03d}",
                    candidate_b_id=f"candidate-b-{index:03d}",
                    feature_delta={"signal": signal, "noise": noise},
                    target_a_probability=0.95 if prefers_a else 0.05,
                    sample_weight=0.8,
                    label_tier=CalibrationLabelTier.AI_JURY_PROVISIONAL,
                )
            )
            index += 1
    return rows


def _protocol() -> WeightTrainingProtocol:
    return WeightTrainingProtocol(
        feature_names=["signal", "noise"],
        non_negative_features=["signal", "noise"],
        hard_gate_features=["cross_scope_exclusion", "future_information_exclusion"],
        baseline_weights={"signal": 0.0, "noise": 1.0},
        min_train_repositories=3,
        iterations=1800,
        required_label_tiers=[CalibrationLabelTier.AI_JURY_PROVISIONAL],
    )


def test_nonnegative_pairwise_training_produces_candidate_only_profile() -> None:
    observations = _observations()

    first = train_candidate_weights(observations, _protocol())
    second = train_candidate_weights(observations, _protocol())

    assert first == second
    assert first.weights["signal"] > 0
    assert first.weights["noise"] >= 0
    assert first.weights["signal"] > first.weights["noise"]
    assert first.selected_l2 in _protocol().l2_candidates
    assert first.metrics["test"].rows == 0
    assert first.metrics["dev"].decisive_accuracy == 1.0
    assert first.candidate_beats_baseline_on_dev is True
    assert first.production_eligible is False
    assert len(first.profile_sha256) == 64
    assert len(first.observations_sha256) == 64


def test_ai_jury_probability_and_uncertainty_become_weak_supervision() -> None:
    jury = AIJuryPairResult(
        comparison_id="comparison",
        query_id="query",
        candidate_a_id="candidate-a",
        candidate_b_id="candidate-b",
        status=JuryDecisionStatus.UNCERTAIN,
        decision=PairwisePreference.ABSTAIN,
        model_families_seen=3,
        effective_model_families=3,
        providers_seen=3,
        effective_providers=3,
        judge_pairs_expected=3,
        judge_pairs_complete=3,
        swap_consistent_pairs=3,
        swap_inconsistent_pairs=0,
        abstained_pairs=0,
        swap_coverage=1.0,
        swap_consistency_rate=1.0,
        probability_a=0.55,
        probability_b=0.35,
        probability_tie=0.1,
        normalized_entropy=0.8,
        training_weight=0.2,
        reliability_source="uniform_unverified",
    )

    observation = observation_from_ai_jury(
        jury,
        observation_id="jury-observation",
        repository_id="train-a",
        partition=CalibrationPartition.TRAIN,
        feature_delta={"signal": 1.0, "noise": 0.0},
    )

    assert observation.target_a_probability == pytest.approx(0.6)
    assert observation.sample_weight == 0.2
    assert observation.label_tier is CalibrationLabelTier.AI_JURY_PROVISIONAL

    automatic = observations_from_ai_jury(
        [jury],
        [
            CalibrationCandidateFeatureVector(
                query_id="query",
                repository_id="train-a",
                partition=CalibrationPartition.TRAIN,
                candidate_id="candidate-a",
                features={"signal": 1.0, "noise": 0.25},
                trace_sha256="a" * 64,
            ),
            CalibrationCandidateFeatureVector(
                query_id="query",
                repository_id="train-a",
                partition=CalibrationPartition.TRAIN,
                candidate_id="candidate-b",
                features={"signal": 0.0, "noise": 0.5},
                trace_sha256="b" * 64,
            ),
        ],
    )
    assert automatic[0].feature_delta == {"signal": 1.0, "noise": -0.25}
    assert automatic[0].id.startswith("jury-")


def test_training_rejects_repository_leakage_and_learned_safety_gates() -> None:
    observations = _observations()
    with pytest.raises(ValueError, match="observation IDs must be unique"):
        train_candidate_weights([*observations, observations[0]], _protocol())

    leaked = observations[0].model_copy(update={"repository_id": "dev"})
    with pytest.raises(ValueError, match="holdout violated"):
        train_candidate_weights([leaked, *observations[1:]], _protocol())

    sealed = observations[-1].model_copy(
        update={
            "id": "sealed-test-observation",
            "repository_id": "sealed-test",
            "partition": CalibrationPartition.TEST,
        }
    )
    with pytest.raises(ValueError, match="must not inspect sealed test"):
        train_candidate_weights([*observations, sealed], _protocol())

    fixture = observations[0].model_copy(
        update={
            "label_tier": CalibrationLabelTier.EXECUTABLE_FIXTURE,
            "execution_agent_model": "fixture",
        }
    )
    with pytest.raises(ValueError, match="validate plumbing"):
        train_candidate_weights([fixture, *observations[1:]], _protocol())

    real_only_in_dev = observations[-1].model_copy(
        update={
            "label_tier": CalibrationLabelTier.EXECUTABLE_REAL,
            "execution_agent_model": "real-agent",
        }
    )
    mixed_tier_protocol = _protocol().model_copy(
        update={
            "required_label_tiers": [
                CalibrationLabelTier.AI_JURY_PROVISIONAL,
                CalibrationLabelTier.EXECUTABLE_REAL,
            ]
        }
    )
    with pytest.raises(ValueError, match="training partition is missing"):
        train_candidate_weights(
            [*observations[:-1], real_only_in_dev],
            mixed_tier_protocol,
        )

    with pytest.raises(ValidationError, match="hard safety gates"):
        WeightTrainingProtocol(
            feature_names=["signal", "cross_scope_exclusion"],
            non_negative_features=["signal"],
            hard_gate_features=["cross_scope_exclusion"],
        )

    with pytest.raises(ValidationError, match="both sign constraints"):
        WeightTrainingProtocol(
            feature_names=["signal"],
            non_negative_features=["signal"],
            non_positive_features=["signal"],
        )


def _production_profile() -> LearnedWeightProfile:
    observations = []
    for index, source in enumerate(_observations()):
        features = {name: 0.0 for name in CALIBRATABLE_FEATURES}
        features["fts_reciprocal_rank"] = source.feature_delta["signal"]
        updates: dict[str, Any] = {"feature_delta": features}
        if index == 0:
            updates.update(
                {
                    "label_tier": CalibrationLabelTier.EXECUTABLE_REAL,
                    "execution_agent_model": "training-agent",
                }
            )
        observations.append(source.model_copy(update=updates))
    protocol = default_weight_training_protocol().model_copy(
        update={"iterations": 500, "l2_candidates": [0.08]}
    )
    return train_candidate_weights(observations, protocol)


def _promotion_rows(profile: LearnedWeightProfile) -> list[WeightCandidateEvaluation]:
    scoring_sha256 = shadow_scoring_profile_from_learned(profile).digest()
    return [
        WeightCandidateEvaluation(
            evaluation_id=f"evaluation-{index:03d}",
            task_id=f"task-{index:03d}",
            repository_id=f"held-out-{index % 3}",
            sequence_id=f"sequence-{index % 10}",
            repeat_id="repeat-1",
            base_commit="b" * 40,
            candidate_profile_sha256=profile.profile_sha256,
            candidate_scoring_profile_sha256=scoring_sha256,
            baseline_profile_id="production-frozen-v2.1",
            prompt_sha256=f"{index:064x}",
            runtime_sha256="d" * 64,
            agent_model="unseen-agent",
            evidence_type=AgentEvidenceType.REAL_CODING_AGENT,
            sealed=True,
            protocol_valid=True,
            baseline_success=False,
            candidate_success=True,
            baseline_safety_violations=0,
            candidate_safety_violations=0,
            baseline_latency_seconds=1.0,
            candidate_latency_seconds=1.1,
            baseline_cost_usd=0.01,
            candidate_cost_usd=0.011,
        )
        for index in range(50)
    ]


def test_weight_promotion_requires_sealed_real_outcomes_and_never_auto_activates() -> None:
    profile = _production_profile()

    decision = evaluate_weight_candidate(profile, _promotion_rows(profile))

    assert decision.approved is True
    assert decision.status == "approved_for_atomic_activation"
    assert decision.success_effect is not None
    assert decision.success_effect["ci95_low"] == 1.0
    assert decision.activates_automatically is False

    fixture_rows = [
        row.model_copy(update={"evidence_type": AgentEvidenceType.DETERMINISTIC_FIXTURE})
        for row in _promotion_rows(profile)
    ]
    rejected = evaluate_weight_candidate(
        profile,
        fixture_rows,
        protocol=WeightPromotionProtocol(),
    )
    assert rejected.approved is False
    assert "real_coding_agent" in " ".join(rejected.reasons)

    first_agent = _promotion_rows(profile)
    incomplete_second_agent = [
        row.model_copy(
            update={
                "evaluation_id": f"agent-two-{row.evaluation_id}",
                "agent_model": "unseen-agent-two",
                "runtime_sha256": "e" * 64,
            }
        )
        for row in first_agent[:-1]
    ]
    incomplete_matrix = evaluate_weight_candidate(
        profile,
        [*first_agent, *incomplete_second_agent],
        protocol=WeightPromotionProtocol(min_agent_models=2),
    )
    assert incomplete_matrix.approved is False
    assert "every sealed task" in " ".join(incomplete_matrix.reasons)

    tampered = profile.model_copy(update={"weights": {"signal": 999.0, "noise": 0.0}})
    with pytest.raises(ValueError, match="profile_sha256"):
        evaluate_weight_candidate(
            tampered,
            _promotion_rows(profile),
        )


def test_weight_shadow_reports_bind_runtime_prompt_and_scoring_projection() -> None:
    profile = _production_profile()
    scoring_sha256 = shadow_scoring_profile_from_learned(profile).digest()
    task = WorkloadTaskSpec.model_validate(
        {
            "id": "sealed-task",
            "repository_id": "held-out",
            "sequence_id": "sealed-sequence",
            "sequence_index": 1,
            "base_commit": "b" * 40,
            "solution_commit": "c" * 40,
            "cutoff": "2026-01-01T00:00:00Z",
            "source_url": "https://example.com/task",
            "source_published_at": "2025-12-31T00:00:00Z",
            "prompt": "Implement the sealed behavior.",
            "memory_seed_ids": [],
            "hidden_test": {
                "image": "python@sha256:" + "a" * 64,
                "command": ["python", "test.py"],
                "hidden_patch": "hidden.patch",
                "hidden_patch_sha256": "f" * 64,
            },
        }
    )

    def report(*, candidate: bool) -> dict[str, Any]:
        profile_sha = scoring_sha256 if candidate else None
        return {
            "run_id": "candidate-run" if candidate else "baseline-run",
            "runtime_spec_sha256": "c" * 64,
            "scoring_profile_sha256": profile_sha,
            "manifest": {
                "name": "sealed-manifest",
                "tier": "public_replay",
                "digest": "a" * 64,
                "schema_version": "2.2",
            },
            "runtime": {
                "provider": "provider",
                "model": "unseen-agent",
                "evidence_type": "real_coding_agent",
            },
            "records": [
                {
                    "task_id": "sealed-task",
                    "repository_id": "held-out",
                    "sequence_id": "sealed-sequence",
                    "condition": "memoryos",
                    "prompt_sha256": "d" * 64,
                    "scoring_profile_sha256": profile_sha,
                    "retrieval_config_hashes": [
                        retrieval_config_hash(shadow_scoring_profile_from_learned(profile))
                        if candidate
                        else retrieval_config_hash()
                    ],
                    "execution_valid": True,
                    "memory_usage_valid": True,
                    "hidden_test_setup_valid": True,
                    "agent_completed": True,
                    "hidden_test_success": candidate,
                    "cross_project_leaks": 0,
                    "stale_memory_uses": 0,
                    "latency_seconds": 1.0,
                    "cost_usd": 0.01,
                }
            ],
        }

    baseline = report(candidate=False)
    candidate = report(candidate=True)
    evaluation = weight_evaluation_from_reports(
        profile,
        baseline,
        candidate,
        task,
        repeat_id="repeat-1",
        sealed=True,
    )

    assert evaluation.protocol_valid is True
    assert evaluation.baseline_success is False
    assert evaluation.candidate_success is True
    assert evaluation.candidate_scoring_profile_sha256 == scoring_sha256

    candidate["scoring_profile_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="expected shadow profile"):
        weight_evaluation_from_reports(
            profile,
            baseline,
            candidate,
            task,
            repeat_id="repeat-1",
            sealed=True,
        )
