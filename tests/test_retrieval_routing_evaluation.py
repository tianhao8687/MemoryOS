from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest

from memoryos.evaluation.real_workload_models import WorkloadTaskSpec
from memoryos.evaluation.retrieval_routing_evaluation import (
    RoutingCandidateEvaluation,
    RoutingPromotionProtocol,
    evaluate_routing_candidate,
    routing_evaluation_from_reports,
)
from memoryos.retrieval_v2.pipeline import RRF_K, RRF_WEIGHTS, retrieval_config_hash
from memoryos.retrieval_v2.routing import (
    APPROVED_RETRIEVAL_RECIPES,
    RETRIEVAL_CHANNELS,
    ROUTER_VERSION,
    SAFE_RECIPE_ID,
    RetrievalRoutingShadowProfile,
)


def _task() -> WorkloadTaskSpec:
    return WorkloadTaskSpec.model_validate(
        {
            "id": "routing-task",
            "repository_id": "held-out",
            "sequence_id": "routing-sequence",
            "sequence_index": 0,
            "base_commit": "b" * 40,
            "cutoff": "2026-01-01T00:00:00Z",
            "prompt": "Locate compile_context and update it.",
            "memory_seed_ids": ["decision"],
            "hidden_test": {
                "image": "python@sha256:" + "a" * 64,
                "command": ["python", "test.py"],
                "hidden_patch": "hidden.patch",
                "hidden_patch_sha256": "f" * 64,
            },
        }
    )


def _route(*, candidate: bool) -> dict[str, Any]:
    recommended = APPROVED_RETRIEVAL_RECIPES["exact-symbol-v1"]
    executed = recommended if candidate else APPROVED_RETRIEVAL_RECIPES[SAFE_RECIPE_ID]
    channel_execution = []
    for channel in RETRIEVAL_CHANNELS:
        requested = channel in executed.channels
        applicable = channel in {"fts", "vector"} or (candidate and channel == "source_anchor")
        actually_executed = requested and applicable
        channel_execution.append(
            {
                "channel": channel,
                "requested": requested,
                "available": True,
                "attempted": actually_executed,
                "executed": actually_executed,
                "candidate_count": int(actually_executed),
                "eligible_candidate_count": int(actually_executed),
                "status": (
                    "executed"
                    if actually_executed
                    else "not_applicable"
                    if requested
                    else "not_requested"
                ),
                "reason_code": (
                    "no_entities"
                    if requested and channel == "graph"
                    else "non_temporal_intent"
                    if requested and channel == "temporal"
                    else None
                ),
                "shared_stage": (
                    "lexical_vector" if actually_executed and channel in {"fts", "vector"} else None
                ),
                "duration_ms": 0.0 if actually_executed else None,
            }
        )
    executed_channels = [item["channel"] for item in channel_execution if item["executed"]]
    return {
        "retrieval_index": 0,
        "route": recommended.route.value,
        "recommended_recipe_id": recommended.recipe_id,
        "recommended_recipe_sha256": recommended.digest(),
        "fallback_used": False,
        "decision_basis": "explicit_signals",
        "reason_codes": ["exact_identifier_or_location"],
        "features": {
            "intent_reason_code": "implementation_keyword",
            "exact_term_count": 1,
            "entity_count": 0,
            "has_exact_signal": True,
            "has_relational_signal": False,
            "has_temporal_signal": False,
            "clause_count": 1,
        },
        "router_version": ROUTER_VERSION,
        "execution_mode": "candidate_shadow" if candidate else "frozen_production_baseline",
        "executed_recipe_id": executed.recipe_id,
        "executed_recipe_sha256": executed.digest(),
        "active_channels": list(executed.channels),
        "requested_channels": list(executed.channels),
        "executed_channels": executed_channels,
        "contributing_channels": executed_channels,
        "degraded_channels": [],
        "channel_execution": channel_execution,
        "fusion": executed.fusion,
        "fusion_weights": {
            channel: RRF_WEIGHTS["fts"] if channel == "source_anchor" else RRF_WEIGHTS[channel]
            for channel in executed.channels
        },
        "rrf_k": RRF_K,
        "score_contract": ("normalized_weighted_rrf_v1" if candidate else "legacy_raw_rrf_v1"),
        "source_anchor_weight_policy": "inherit_fts" if candidate else None,
        "reranker_policy": executed.reranker_policy,
        "reranker_mode": "disabled",
        "diversity_policy": executed.diversity_policy,
        "candidate_pool_min": executed.candidate_pool_min,
        "candidate_pool_max": executed.candidate_pool_max,
        "rerank_window": executed.rerank_window,
        "fallback_recipe_id": SAFE_RECIPE_ID,
        "stage_timings_ms": {
            "candidate_retrieval": 0.0,
            "fusion": 0.0,
            "governance_scoring": 0.0,
            "rerank": 0.0,
            "diversity": 0.0,
        },
    }


def _report(
    profile: RetrievalRoutingShadowProfile,
    *,
    candidate: bool,
) -> dict[str, Any]:
    profile_sha = profile.digest() if candidate else None
    return {
        "run_id": "candidate-run" if candidate else "baseline-run",
        "runtime_spec_sha256": "c" * 64,
        "scoring_profile_sha256": None,
        "routing_profile_sha256": profile_sha,
        "manifest": {
            "name": "routing-manifest",
            "tier": "public_replay",
            "digest": "a" * 64,
            "schema_version": "2.2",
        },
        "runtime": {
            "provider": "provider",
            "model": "agent-model",
            "evidence_type": "real_coding_agent",
        },
        "records": [
            {
                "task_id": "routing-task",
                "repository_id": "held-out",
                "sequence_id": "routing-sequence",
                "condition": "memoryos",
                "prompt_sha256": "d" * 64,
                "scoring_profile_sha256": None,
                "routing_profile_sha256": profile_sha,
                "retrieval_runs": 1,
                "retrieval_config_hashes": [
                    retrieval_config_hash(routing_profile=profile)
                    if candidate
                    else retrieval_config_hash()
                ],
                "retrieval_routes": [_route(candidate=candidate)],
                "selected_seed_ids": ["decision"],
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


def test_routing_evaluation_proves_baseline_and_candidate_execution() -> None:
    profile = RetrievalRoutingShadowProfile()
    evaluation = routing_evaluation_from_reports(
        profile,
        _report(profile, candidate=False),
        _report(profile, candidate=True),
        _task(),
        repeat_id="repeat-1",
    )

    assert evaluation.protocol_valid is True
    assert evaluation.baseline_success is False
    assert evaluation.candidate_success is True
    assert evaluation.recommended_recipe_counts == {"exact-symbol-v1": 1}
    assert evaluation.executed_recipe_counts == {"exact-symbol-v1": 1}
    assert evaluation.routing_profile_sha256 == profile.digest()
    assert evaluation.production_eligible is False


def test_routing_evaluation_rejects_declared_profile_without_routed_execution() -> None:
    profile = RetrievalRoutingShadowProfile()
    candidate = _report(profile, candidate=True)
    candidate["records"][0]["retrieval_routes"][0] = _route(candidate=False)

    with pytest.raises(ValueError, match="candidate_shadow"):
        routing_evaluation_from_reports(
            profile,
            _report(profile, candidate=False),
            candidate,
            _task(),
            repeat_id="repeat-1",
        )


def test_routing_evaluation_rejects_tampered_recipe_digest() -> None:
    profile = RetrievalRoutingShadowProfile()
    candidate = deepcopy(_report(profile, candidate=True))
    candidate["records"][0]["retrieval_routes"][0]["executed_recipe_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="executed recipe digest"):
        routing_evaluation_from_reports(
            profile,
            _report(profile, candidate=False),
            candidate,
            _task(),
            repeat_id="repeat-1",
        )


def test_routing_evaluation_rejects_recipe_unsupported_by_features() -> None:
    profile = RetrievalRoutingShadowProfile()
    candidate = deepcopy(_report(profile, candidate=True))
    candidate["records"][0]["retrieval_routes"][0]["features"]["has_exact_signal"] = False

    with pytest.raises(ValueError, match="features do not support"):
        routing_evaluation_from_reports(
            profile,
            _report(profile, candidate=False),
            candidate,
            _task(),
            repeat_id="repeat-1",
        )


def _promotion_evaluation(
    profile: RetrievalRoutingShadowProfile,
    *,
    task_id: str,
    repository_id: str,
    sequence_id: str,
    agent_model: str,
    recipe_id: str,
    baseline_success: bool,
    candidate_success: bool,
) -> RoutingCandidateEvaluation:
    base = routing_evaluation_from_reports(
        profile,
        _report(profile, candidate=False),
        _report(profile, candidate=True),
        _task(),
        repeat_id="repeat-1",
    )
    identity = f"{task_id}-{agent_model}"
    identity_sha = hashlib.sha256(identity.encode()).hexdigest()
    return base.model_copy(
        update={
            "evaluation_id": identity,
            "task_id": task_id,
            "repository_id": repository_id,
            "sequence_id": sequence_id,
            "agent_model": agent_model,
            "runtime_sha256": hashlib.sha256(f"runtime-{agent_model}".encode()).hexdigest(),
            "baseline_run_id": f"baseline-{identity}",
            "candidate_run_id": f"candidate-{identity}",
            "manifest_sha256": hashlib.sha256(f"manifest-{task_id}".encode()).hexdigest(),
            "task_spec_sha256": hashlib.sha256(f"task-{task_id}".encode()).hexdigest(),
            "baseline_report_sha256": hashlib.sha256(
                f"baseline-{identity_sha}".encode()
            ).hexdigest(),
            "candidate_report_sha256": hashlib.sha256(
                f"candidate-{identity_sha}".encode()
            ).hexdigest(),
            "sealed": True,
            "baseline_success": baseline_success,
            "candidate_success": candidate_success,
            "recommended_recipe_counts": {recipe_id: 1},
            "executed_recipe_counts": {recipe_id: 1},
            "fallback_count": int(recipe_id == SAFE_RECIPE_ID),
        }
    )


def test_routing_promotion_aggregates_tasks_not_repeated_agent_rows() -> None:
    profile = RetrievalRoutingShadowProfile()
    evaluations = [
        _promotion_evaluation(
            profile,
            task_id=task_id,
            repository_id=f"repo-{task_index}",
            sequence_id=f"sequence-{task_index}",
            agent_model=agent,
            recipe_id=("exact-symbol-v1" if task_index == 1 else "semantic-hybrid-v1"),
            baseline_success=False,
            candidate_success=True,
        )
        for task_index, task_id in enumerate(("task-1", "task-2"), start=1)
        for agent in ("agent-a", "agent-b")
    ]
    protocol = RoutingPromotionProtocol(
        min_tasks=2,
        min_repositories=2,
        min_sequences=2,
        min_agent_models=2,
        min_tasks_per_agent_model=2,
        min_distinct_recipes=2,
        min_tasks_per_recipe=1,
    )

    decision = evaluate_routing_candidate(profile, evaluations, protocol=protocol)

    assert decision.status == "eligible_for_sealed_activation_review"
    assert decision.task_count == 2
    assert decision.agent_model_count == 2
    assert decision.success_difference is not None
    assert decision.success_difference["difference"] == 1.0
    assert len(decision.evaluation_set_sha256) == 64
    assert decision.bootstrap_seed == 20260813
    assert decision.production_activated is False


def test_routing_promotion_rejects_worst_recipe_regression() -> None:
    profile = RetrievalRoutingShadowProfile()
    evaluations = [
        _promotion_evaluation(
            profile,
            task_id="task-helped",
            repository_id="repo-a",
            sequence_id="sequence-a",
            agent_model="agent-a",
            recipe_id="semantic-hybrid-v1",
            baseline_success=False,
            candidate_success=True,
        ),
        _promotion_evaluation(
            profile,
            task_id="task-harmed",
            repository_id="repo-b",
            sequence_id="sequence-b",
            agent_model="agent-a",
            recipe_id="exact-symbol-v1",
            baseline_success=True,
            candidate_success=False,
        ),
    ]
    protocol = RoutingPromotionProtocol(
        min_tasks=2,
        min_repositories=2,
        min_sequences=2,
        min_agent_models=1,
        min_tasks_per_agent_model=2,
        min_distinct_recipes=2,
        min_tasks_per_recipe=1,
        success_ci95_low_must_exceed=-1.0,
    )

    decision = evaluate_routing_candidate(profile, evaluations, protocol=protocol)

    assert decision.status == "retain_frozen_baseline"
    assert decision.worst_recipe_success_delta == -1.0
    assert "worst-recipe success regressed" in decision.reasons


def test_routing_promotion_revalidates_programmatically_copied_evidence() -> None:
    profile = RetrievalRoutingShadowProfile()
    valid = _promotion_evaluation(
        profile,
        task_id="task-1",
        repository_id="repo-a",
        sequence_id="sequence-a",
        agent_model="agent-a",
        recipe_id="semantic-hybrid-v1",
        baseline_success=False,
        candidate_success=True,
    )
    invalid = valid.model_copy(update={"executed_recipe_counts": {"exact-symbol-v1": 1}})

    with pytest.raises(ValueError, match="candidate execution must match"):
        evaluate_routing_candidate(
            profile,
            [invalid],
            protocol=RoutingPromotionProtocol(
                min_tasks=1,
                min_repositories=1,
                min_sequences=1,
                min_agent_models=1,
                min_tasks_per_agent_model=1,
                min_distinct_recipes=1,
                min_tasks_per_recipe=1,
            ),
        )
