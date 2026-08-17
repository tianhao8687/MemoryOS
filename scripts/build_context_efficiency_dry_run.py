from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from memoryos.config import settings_for
from memoryos.context.budget import BudgetPlanner
from memoryos.context.token_meter import UnicodeHeuristicTokenCounter
from memoryos.domain.schemas import MemoryOperationTokenAttribution
from memoryos.evaluation.context_efficiency import (
    MEMORYOS_CONTEXT_CONDITIONS,
    ContextEfficiencyCondition,
    ContextEfficiencyConfig,
    ContextEfficiencyMode,
    ContextEfficiencyRecord,
    ContextEfficiencyStudyBuilder,
    DeltaThresholdResult,
    ProviderTokenAttribution,
    tokenizer_evidence_sha256,
    write_context_efficiency_report,
)
from memoryos.mcp_server.server import create_mcp_server
from memoryos.mcp_server.tool_registry import ToolProfile, server_schema_snapshot

FIXED_STARTED_AT = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def build_report(root: Path) -> dict[str, object]:
    counter = UnicodeHeuristicTokenCounter()
    config = ContextEfficiencyConfig(
        frozen_at=FIXED_STARTED_AT - timedelta(hours=1),
        conditions=MEMORYOS_CONTEXT_CONDITIONS,
        bootstrap_rounds=1000,
    )
    with tempfile.TemporaryDirectory(
        prefix="memoryos-context-study-",
        ignore_cleanup_errors=True,
    ) as directory:
        snapshots = {}
        for profile in ToolProfile:
            profile_settings = settings_for(Path(directory) / profile.value)
            snapshots[profile.value] = await server_schema_snapshot(
                create_mcp_server(profile_settings, profile),
                profile=profile,
                counter=counter,
            )
        settings = settings_for(Path(directory) / "policy")
        policy_hash = BudgetPlanner(settings, counter).policy_hash
        logging.shutdown()
    dataset_path = root / "docs" / "verification" / "v2.3" / "v22-context-compiler-golden.json"
    dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    records: list[ContextEfficiencyRecord] = []
    for task_index in range(3):
        task_id = f"dry-task-{task_index}"
        for condition in config.condition_order(task_id=task_id, task_ordinal=task_index):
            schema = (
                snapshots[ToolProfile.CORE.value]
                if condition is ContextEfficiencyCondition.MSC_DELTA_CORE
                else snapshots[ToolProfile.ALL.value]
            )
            delta_condition = condition in {
                ContextEfficiencyCondition.MSC_DELTA,
                ContextEfficiencyCondition.MSC_DELTA_CORE,
            }
            delta_threshold_results = (
                tuple(
                    DeltaThresholdResult(
                        threshold=threshold,
                        policy_sha256=hashlib.sha256(
                            f"{policy_hash}:{condition.value}:{threshold}".encode()
                        ).hexdigest(),
                        patch_sha256=hashlib.sha256(
                            f"dry-threshold-patch:{task_index}:{condition.value}:{threshold}".encode()
                        ).hexdigest(),
                        execution_valid=True,
                        agent_completed=True,
                        hidden_test_success=True,
                        provider_input_tokens=None,
                        provider_output_tokens=None,
                        memory_delivery_payload_tokens=(120 if threshold > 0.5 else 240),
                        memory_delta_tokens=120,
                        memory_full_equivalent_tokens=240,
                        delta_hits=1 if threshold > 0.5 else 0,
                        full_fallbacks=0 if threshold > 0.5 else 1,
                    )
                    for threshold in config.delta_thresholds
                )
                if delta_condition
                else ()
            )
            legacy = condition is ContextEfficiencyCondition.LEGACY_FULL
            records.append(
                ContextEfficiencyRecord(
                    task_id=task_id,
                    repository_id=f"dry-repo-{task_index}",
                    sequence_id=f"dry-sequence-{task_index}",
                    sequence_index=task_index,
                    is_cross_step=task_index > 0,
                    intent="current_decision",
                    agent_version="deterministic-protocol-fixture-v1",
                    model="no-provider-model",
                    image_digest="a" * 64,
                    runtime_sha256="9" * 64,
                    evidence_type="deterministic_fixture",
                    dataset_tier="fixture",
                    condition=condition,
                    execution_index=len(records),
                    prompt_sha256=hashlib.sha256(f"dry-task-{task_index}".encode()).hexdigest(),
                    starting_state_sha256=hashlib.sha256(
                        f"dry-state-{task_index}".encode()
                    ).hexdigest(),
                    patch_sha256=hashlib.sha256(f"dry-patch-{task_index}".encode()).hexdigest(),
                    study_config_sha256=config.digest(),
                    policy_sha256=policy_hash,
                    tool_profile=str(schema["profile"]),
                    tool_schema_sha256=str(schema["schema_sha256"]),
                    dataset_sha256=dataset_hash,
                    tokenizer_id=counter.tokenizer_id,
                    counter_kind="estimated",
                    counter_version=counter.counter_version,
                    tokenizer_sha256=tokenizer_evidence_sha256(
                        tokenizer_id=counter.tokenizer_id,
                        counter_kind=counter.kind.value,
                        counter_version=counter.counter_version,
                    ),
                    provider_token_attribution=ProviderTokenAttribution.UNAVAILABLE,
                    provider_input_tokens=None,
                    provider_output_tokens=None,
                    cost_usd=None,
                    latency_seconds=0,
                    agent_completed=True,
                    hidden_test_success=True,
                    execution_valid=True,
                    memory_context_text_tokens=320 if legacy else 90 if delta_condition else 120,
                    memory_delivery_payload_tokens=(
                        400 if legacy else 120 if delta_condition else 180
                    ),
                    memory_payload_overhead_tokens=80 if legacy else 30 if delta_condition else 60,
                    memory_evidence_tokens=0,
                    memory_history_tokens=0,
                    memory_delta_tokens=120 if delta_condition else 0,
                    memory_full_equivalent_tokens=400 if legacy else 240,
                    context_compilation_llm_input_tokens=0,
                    context_compilation_llm_output_tokens=0,
                    other_memory_operation_llm_input_tokens=0,
                    other_memory_operation_llm_output_tokens=0,
                    other_memory_operation_token_attribution=(
                        MemoryOperationTokenAttribution.EXACT_ZERO
                    ),
                    memory_tool_schema_tokens=int(schema["estimated_schema_tokens"]),
                    memory_tool_calls=1,
                    delta_hits=1 if delta_condition else 0,
                    delta_threshold_results=delta_threshold_results,
                )
            )
    report = ContextEfficiencyStudyBuilder(config).build(
        records,
        mode=ContextEfficiencyMode.DRY_RUN,
        run_id="context-efficiency-deterministic-dry-run-v1",
        started_at=FIXED_STARTED_AT,
        finished_at=FIXED_STARTED_AT + timedelta(minutes=1),
    )
    report["evidence_level"] = "deterministic_protocol_fixture"
    report["schema_snapshots"] = snapshots
    report["confirmatory_status"] = {
        "state": "evidence_pending",
        "provider_usage_available": False,
        "real_agent_tasks_completed": 0,
        "required_minimum_tasks": config.minimum_tasks,
        "power_required_tasks": config.required_sample_size(),
        "default_compiler_mode": "legacy",
        "effect_claim": "none",
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/v2.3/context-efficiency-dry-run.json"),
    )
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(build_report(root))
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    write_context_efficiency_report(output, report)


if __name__ == "__main__":
    main()
