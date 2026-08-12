from __future__ import annotations

import pytest

from memoryos.evaluation.executable_ablation import (
    AblationArm,
    AblationEffectStatus,
    ExecutableAblationRun,
    ablation_run_from_report,
    analyze_executable_ablations,
    build_ablation_plan,
    materialize_ablation_manifest,
    materialize_task_manifest,
)
from memoryos.evaluation.real_workload_agent import AgentEvidenceType
from memoryos.evaluation.real_workload_models import RealWorkloadManifest
from memoryos.evaluation.retrieval_weight_calibration import (
    CalibrationLabelTier,
    CalibrationPartition,
    observation_from_ablation_pair,
)

IMAGE = "python@sha256:" + "a" * 64


def _candidate_trace() -> dict[str, object]:
    return {
        "fts_rank": 1,
        "vector_rank": None,
        "graph_rank": None,
        "temporal_rank": 2,
        "freshness": "fresh",
        "scope_match": "repository",
        "truth_state": "resolved",
        "evidence_count": 1,
        "helpful_feedback_count": 0,
        "unhelpful_feedback_count": 0,
        "memory_confidence": 0.9,
        "memory_importance": 0.8,
        "reranker_score": None,
    }


def _manifest() -> RealWorkloadManifest:
    return RealWorkloadManifest.model_validate(
        {
            "name": "ablation-fixture",
            "tier": "harness_fixture",
            "generated_at": "2026-08-12T00:00:00Z",
            "repositories": [{"id": "repo", "clone_url": "fixture/repo", "license_spdx": "MIT"}],
            "memories": [
                {
                    "id": "useful",
                    "repository_id": "repo",
                    "category": "decision",
                    "title": "Useful decision",
                    "content": "Use the supported implementation.",
                    "captured_at": "2026-01-01T00:00:00Z",
                    "source_ref": "fixture",
                    "expectation": "helpful",
                },
                {
                    "id": "stale",
                    "repository_id": "repo",
                    "category": "decision",
                    "title": "Expired decision",
                    "content": "Old behavior STALE-ABLATION-CANARY.",
                    "captured_at": "2025-01-01T00:00:00Z",
                    "valid_to": "2025-02-01T00:00:00Z",
                    "source_ref": "fixture",
                    "expectation": "stale",
                    "canary": "STALE-ABLATION-CANARY",
                },
            ],
            "tasks": [
                {
                    "id": "task",
                    "repository_id": "repo",
                    "sequence_id": "sequence",
                    "sequence_index": 1,
                    "base_commit": "b" * 40,
                    "cutoff": "2026-02-01T00:00:00Z",
                    "prompt": "Implement the supported behavior.",
                    "memory_seed_ids": ["useful", "stale"],
                    "hidden_test": {"image": IMAGE, "command": ["python", "test.py"]},
                }
            ],
        }
    )


def _run(
    *,
    repeat: str,
    arm: AblationArm,
    success: bool,
    selected: list[str],
    excluded: str | None = None,
    evidence: AgentEvidenceType = AgentEvidenceType.REAL_CODING_AGENT,
) -> ExecutableAblationRun:
    return ExecutableAblationRun(
        run_id=f"run-{repeat}-{arm.value}",
        task_id="task",
        repository_id="repo",
        base_commit="b" * 40,
        prompt_sha256="c" * 64,
        agent_family="provider",
        agent_model="coding-model",
        runtime_sha256="d" * 64,
        repeat_id=repeat,
        evidence_type=evidence,
        arm=arm,
        excluded_memory_id=excluded,
        protocol_valid=True,
        agent_completed=True,
        hidden_test_success=success,
        selected_memory_ids=selected,
        cross_project_leaks=0,
        stale_memory_uses=0,
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.01,
        latency_seconds=2.0,
    )


def test_ablation_plan_targets_selected_eligible_memories_only() -> None:
    manifest = _manifest()

    plan = build_ablation_plan(manifest, {"task": ["useful", "stale"]})

    assert [item.excluded_memory_id for item in plan] == ["useful"]
    derived = materialize_ablation_manifest(
        manifest,
        task_id="task",
        excluded_memory_id="useful",
    )
    assert derived.tasks[0].memory_seed_ids == ["stale"]
    assert [memory.id for memory in derived.memories] == ["stale"]

    full = materialize_task_manifest(manifest, task_id="task")
    assert full.tasks[0].memory_seed_ids == ["useful", "stale"]
    assert [memory.id for memory in full.memories] == ["useful", "stale"]


def test_executable_ablation_estimates_memory_effect_from_paired_repeats() -> None:
    runs = [
        _run(
            repeat="one",
            arm=AblationArm.MEMORYOS_FULL,
            success=True,
            selected=["useful"],
        ),
        _run(
            repeat="one",
            arm=AblationArm.MEMORYOS_MINUS_MEMORY,
            success=False,
            selected=[],
            excluded="useful",
        ),
        _run(
            repeat="two",
            arm=AblationArm.MEMORYOS_FULL,
            success=True,
            selected=["useful"],
        ),
        _run(
            repeat="two",
            arm=AblationArm.MEMORYOS_MINUS_MEMORY,
            success=True,
            selected=[],
            excluded="useful",
        ),
    ]

    report = analyze_executable_ablations(runs)
    effect = report.effects[0]

    assert effect.status is AblationEffectStatus.ESTIMATED
    assert effect.informative_pairs == 2
    assert effect.helped_pairs == 1
    assert effect.harmed_pairs == 0
    assert effect.unchanged_pairs == 1
    assert effect.success_effect is not None
    assert effect.success_effect.difference == 0.5
    assert report.real_agent_effects == 1
    assert report.production_eligible is False


def test_unselected_memory_ablation_is_not_misreported_as_zero_effect() -> None:
    runs = [
        _run(
            repeat="one",
            arm=AblationArm.MEMORYOS_FULL,
            success=True,
            selected=[],
        ),
        _run(
            repeat="one",
            arm=AblationArm.MEMORYOS_MINUS_MEMORY,
            success=True,
            selected=[],
            excluded="useful",
        ),
    ]

    effect = analyze_executable_ablations(runs).effects[0]

    assert effect.status is AblationEffectStatus.NOT_SELECTED
    assert effect.informative_pairs == 0
    assert effect.success_effect is None


def test_ablation_pair_rejects_a_changed_runtime() -> None:
    full = _run(
        repeat="one",
        arm=AblationArm.MEMORYOS_FULL,
        success=True,
        selected=["useful"],
    ).model_copy(update={"candidate_traces": {"useful": _candidate_trace()}})
    minus = _run(
        repeat="one",
        arm=AblationArm.MEMORYOS_MINUS_MEMORY,
        success=False,
        selected=[],
        excluded="useful",
    ).model_copy(update={"runtime_sha256": "e" * 64})

    with pytest.raises(ValueError, match="runtime"):
        analyze_executable_ablations([full, minus])

    with pytest.raises(ValueError, match="duplicate minus-memory"):
        analyze_executable_ablations(
            [
                full,
                minus.model_copy(update={"runtime_sha256": "d" * 64}),
                minus.model_copy(update={"runtime_sha256": "d" * 64}),
            ]
        )


def test_ablation_report_identity_checks_reject_wrong_resumed_arm() -> None:
    report = {
        "manifest": {"digest": "a" * 64},
        "runtime_spec_sha256": "b" * 64,
        "runtime": {
            "provider": "provider",
            "model": "coding-model",
            "evidence_type": "real_coding_agent",
        },
        "run_id": "resumed-run",
        "records": [
            {
                "task_id": "task",
                "condition": "memoryos",
                "prompt_sha256": "c" * 64,
                "execution_valid": True,
                "memory_usage_valid": True,
                "hidden_test_setup_valid": True,
                "agent_completed": True,
                "hidden_test_success": False,
            }
        ],
    }

    with pytest.raises(ValueError, match="manifest digest"):
        ablation_run_from_report(
            report,
            _manifest().tasks[0],
            arm=AblationArm.MEMORYOS_MINUS_MEMORY,
            repeat_id="one",
            excluded_memory_id="useful",
            expected_manifest_digest="d" * 64,
            expected_runtime_sha256="b" * 64,
        )

    with pytest.raises(ValueError, match="runtime digest"):
        ablation_run_from_report(
            report,
            _manifest().tasks[0],
            arm=AblationArm.MEMORYOS_MINUS_MEMORY,
            repeat_id="one",
            excluded_memory_id="useful",
            expected_manifest_digest="a" * 64,
            expected_runtime_sha256="d" * 64,
        )


def test_only_discordant_executable_ablation_pairs_become_training_labels() -> None:
    full = _run(
        repeat="one",
        arm=AblationArm.MEMORYOS_FULL,
        success=True,
        selected=["useful"],
    ).model_copy(update={"candidate_traces": {"useful": _candidate_trace()}})
    minus = _run(
        repeat="one",
        arm=AblationArm.MEMORYOS_MINUS_MEMORY,
        success=False,
        selected=[],
        excluded="useful",
    )

    observation = observation_from_ablation_pair(
        full,
        minus,
        observation_id="ablation-observation",
        partition=CalibrationPartition.TRAIN,
        feature_delta={"signal": 1.0},
    )

    assert observation is not None
    assert observation.target_a_probability == 1.0
    assert observation.label_tier is CalibrationLabelTier.EXECUTABLE_REAL

    automatic = observation_from_ablation_pair(
        full,
        minus,
        observation_id="automatic-ablation-observation",
        partition=CalibrationPartition.TRAIN,
    )
    assert automatic is not None
    assert automatic.feature_delta["fts_reciprocal_rank"] == 1.0
    assert "scope_other" not in automatic.feature_delta

    unchanged = minus.model_copy(update={"hidden_test_success": True})
    assert (
        observation_from_ablation_pair(
            full,
            unchanged,
            observation_id="uninformative",
            partition=CalibrationPartition.TRAIN,
            feature_delta={"signal": 1.0},
        )
        is None
    )


def test_real_workload_report_converts_to_full_ablation_evidence() -> None:
    manifest = _manifest()
    report = {
        "run_id": "source-run",
        "runtime": {
            "provider": "provider",
            "model": "coding-model",
            "evidence_type": "deterministic_fixture",
        },
        "records": [
            {
                "task_id": "task",
                "condition": "memoryos",
                "prompt_sha256": "c" * 64,
                "execution_valid": True,
                "memory_usage_valid": True,
                "hidden_test_setup_valid": True,
                "agent_completed": True,
                "hidden_test_success": True,
                "selected_seed_ids": ["useful"],
                "retrieval_candidate_features": [
                    {
                        "seed_id": "useful",
                        "retrieval_index": 0,
                        "selected": True,
                        "trace": _candidate_trace(),
                    }
                ],
                "cross_project_leaks": 0,
                "stale_memory_uses": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": None,
                "latency_seconds": 1.5,
            }
        ],
    }

    converted = ablation_run_from_report(
        report,
        manifest.tasks[0],
        arm=AblationArm.MEMORYOS_FULL,
        repeat_id="repeat-1",
    )

    assert converted.evidence_type is AgentEvidenceType.DETERMINISTIC_FIXTURE
    assert converted.functional_success is True
    assert converted.selected_memory_ids == ["useful"]
    assert converted.candidate_traces["useful"]["fts_rank"] == 1


def test_report_conversion_recovers_registered_static_memory_features() -> None:
    manifest = _manifest()
    trace = _candidate_trace()
    trace.pop("memory_confidence")
    trace.pop("memory_importance")
    report = {
        "run_id": "legacy-source-run",
        "runtime": {
            "provider": "provider",
            "model": "coding-model",
            "evidence_type": "real_coding_agent",
        },
        "records": [
            {
                "task_id": "task",
                "condition": "memoryos",
                "prompt_sha256": "c" * 64,
                "execution_valid": True,
                "memory_usage_valid": True,
                "hidden_test_setup_valid": True,
                "agent_completed": True,
                "hidden_test_success": True,
                "selected_seed_ids": ["useful"],
                "retrieval_candidate_features": [
                    {
                        "seed_id": "useful",
                        "retrieval_index": 0,
                        "selected": True,
                        "trace": trace,
                    }
                ],
            }
        ],
    }

    converted = ablation_run_from_report(
        report,
        manifest.tasks[0],
        arm=AblationArm.MEMORYOS_FULL,
        repeat_id="repeat-legacy",
        registered_memories=manifest.memories,
    )

    assert converted.candidate_traces["useful"]["memory_confidence"] == 0.9
    assert converted.candidate_traces["useful"]["memory_importance"] == 0.7

    tampered = {
        **report,
        "records": [
            {
                **report["records"][0],
                "retrieval_candidate_features": [
                    {
                        "seed_id": "useful",
                        "retrieval_index": 0,
                        "selected": True,
                        "trace": {**trace, "memory_confidence": 0.1},
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValueError, match="does not match"):
        ablation_run_from_report(
            tampered,
            manifest.tasks[0],
            arm=AblationArm.MEMORYOS_FULL,
            repeat_id="repeat-tampered",
            registered_memories=manifest.memories,
        )
