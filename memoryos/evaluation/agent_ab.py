from __future__ import annotations

import random
from typing import Any

from memoryos.evaluation.metrics import bootstrap_mean_difference


def run_fixture_agent_ab(*, tasks: int = 30, seed: int = 20260810) -> dict[str, Any]:
    """Validate the A/B harness only; these are not real-model effectiveness results."""

    rng = random.Random(seed)  # noqa: S311 - reproducible benchmark fixture, not cryptography
    baseline_success = []
    enabled_success = []
    baseline_repeat = []
    enabled_repeat = []
    baseline_stale = []
    enabled_stale = []
    baseline_compliance = []
    enabled_compliance = []
    baseline_tool_calls = []
    enabled_tool_calls = []
    baseline_context = []
    enabled_context = []
    baseline_latency = []
    enabled_latency = []
    records = []
    for index in range(tasks):
        difficulty = rng.random()
        baseline_ok = 1.0 if difficulty < 0.55 else 0.0
        enabled_ok = 1.0 if difficulty < 0.9 else 0.0
        baseline_mistake = 1.0 if difficulty >= 0.5 else 0.0
        enabled_mistake = 1.0 if difficulty >= 0.88 else 0.0
        baseline_stale_misuse = 1.0 if index % 4 == 0 else 0.0
        enabled_stale_misuse = 0.0
        baseline_decision_compliance = 1.0 if difficulty < 0.62 else 0.0
        enabled_decision_compliance = 1.0 if difficulty < 0.91 else 0.0
        baseline_calls = float(8 + index % 5)
        enabled_calls = float(6 + index % 3)
        baseline_chars = 0.0
        enabled_chars = float(1500 + index % 7 * 100)
        baseline_seconds = 22.0 + index % 6
        enabled_seconds = 20.0 + index % 5
        baseline_success.append(baseline_ok)
        enabled_success.append(enabled_ok)
        baseline_repeat.append(baseline_mistake)
        enabled_repeat.append(enabled_mistake)
        baseline_stale.append(baseline_stale_misuse)
        enabled_stale.append(enabled_stale_misuse)
        baseline_compliance.append(baseline_decision_compliance)
        enabled_compliance.append(enabled_decision_compliance)
        baseline_tool_calls.append(baseline_calls)
        enabled_tool_calls.append(enabled_calls)
        baseline_context.append(baseline_chars)
        enabled_context.append(enabled_chars)
        baseline_latency.append(baseline_seconds)
        enabled_latency.append(enabled_seconds)
        records.append(
            {
                "task_id": f"fixture-agent-task-{index + 1:02d}",
                "seeded_decision": "Use FastAPI and do not introduce Redis.",
                "seeded_failure": "The previous cache worker race must not be repeated.",
                "baseline_success": bool(baseline_ok),
                "memoryos_success": bool(enabled_ok),
                "baseline_repeated_mistake": bool(baseline_mistake),
                "memoryos_repeated_mistake": bool(enabled_mistake),
                "baseline_stale_misuse": bool(baseline_stale_misuse),
                "memoryos_stale_misuse": bool(enabled_stale_misuse),
                "baseline_decision_compliance": bool(baseline_decision_compliance),
                "memoryos_decision_compliance": bool(enabled_decision_compliance),
                "baseline_tool_calls": int(baseline_calls),
                "memoryos_tool_calls": int(enabled_calls),
                "baseline_context_chars": int(baseline_chars),
                "memoryos_context_chars": int(enabled_chars),
                "baseline_latency_seconds": baseline_seconds,
                "memoryos_latency_seconds": enabled_seconds,
            }
        )
    return {
        "sample_size": tasks,
        "evidence_type": "fixture",
        "real_model": False,
        "claim": "harness_validation_only",
        "baseline": {
            "task_success": sum(baseline_success) / tasks,
            "repeated_mistake_rate": sum(baseline_repeat) / tasks,
            "stale_memory_misuse": sum(baseline_stale) / tasks,
            "confirmed_decision_compliance": sum(baseline_compliance) / tasks,
            "unnecessary_tool_calls": sum(baseline_tool_calls) / tasks,
            "context_cost_chars": sum(baseline_context) / tasks,
            "latency_seconds": sum(baseline_latency) / tasks,
        },
        "memoryos_enabled": {
            "task_success": sum(enabled_success) / tasks,
            "repeated_mistake_rate": sum(enabled_repeat) / tasks,
            "stale_memory_misuse": sum(enabled_stale) / tasks,
            "confirmed_decision_compliance": sum(enabled_compliance) / tasks,
            "unnecessary_tool_calls": sum(enabled_tool_calls) / tasks,
            "context_cost_chars": sum(enabled_context) / tasks,
            "latency_seconds": sum(enabled_latency) / tasks,
        },
        "task_success_difference": bootstrap_mean_difference(
            baseline_success, enabled_success, seed=seed
        ),
        "repeated_mistake_difference": bootstrap_mean_difference(
            baseline_repeat, enabled_repeat, seed=seed + 1
        ),
        "decision_compliance_difference": bootstrap_mean_difference(
            baseline_compliance, enabled_compliance, seed=seed + 2
        ),
        "stale_misuse_difference": bootstrap_mean_difference(
            baseline_stale, enabled_stale, seed=seed + 3
        ),
        "unnecessary_tool_calls_difference": bootstrap_mean_difference(
            baseline_tool_calls, enabled_tool_calls, seed=seed + 4
        ),
        "context_cost_difference": bootstrap_mean_difference(
            baseline_context, enabled_context, seed=seed + 5
        ),
        "latency_difference": bootstrap_mean_difference(
            baseline_latency, enabled_latency, seed=seed + 6
        ),
        "interpretation": (
            "Synthetic fixture validates pairing, metrics, and confidence-interval plumbing only; "
            "it is not evidence that a real coding model improves."
        ),
        "records": records,
    }
