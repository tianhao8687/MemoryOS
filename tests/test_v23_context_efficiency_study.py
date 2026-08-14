from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memoryos.evaluation.context_efficiency import (
    ContextEfficiencyCondition,
    ContextEfficiencyConfig,
    ContextEfficiencyMode,
    ContextEfficiencyRecord,
    ContextEfficiencyStudyBuilder,
    DeltaThresholdResult,
    tokenizer_evidence_sha256,
    write_context_efficiency_report,
)
from memoryos.evaluation.real_workload_models import ExperimentCondition
from scripts.build_context_efficiency_dry_run import build_report

ROOT = Path(__file__).resolve().parents[1]


def _config() -> ContextEfficiencyConfig:
    return ContextEfficiencyConfig(
        frozen_at=datetime(2026, 8, 15, tzinfo=UTC),
        bootstrap_rounds=1000,
        worst_group_minimum_n=2,
    )


def _records(
    config: ContextEfficiencyConfig,
    *,
    tasks: int = 2,
    unsafe: bool = False,
) -> list[ContextEfficiencyRecord]:
    records: list[ContextEfficiencyRecord] = []
    for task_index in range(tasks):
        task_id = f"task-{task_index}"
        for condition in config.condition_order(task_id=task_id, task_ordinal=task_index):
            legacy = condition is ContextEfficiencyCondition.LEGACY_FULL
            delta_condition = condition in {
                ContextEfficiencyCondition.MSC_DELTA,
                ContextEfficiencyCondition.MSC_DELTA_CORE,
            }
            records.append(
                ContextEfficiencyRecord(
                    task_id=task_id,
                    repository_id=f"repo-{task_index % 3}",
                    sequence_id=f"sequence-{task_index % 10}",
                    sequence_index=task_index,
                    is_cross_step=True,
                    intent="current_decision",
                    agent_version="fixture-agent",
                    model="fixture-model",
                    image_digest="a" * 64,
                    runtime_sha256="9" * 64,
                    evidence_type="deterministic_fixture",
                    dataset_tier="fixture",
                    condition=condition,
                    execution_index=len(records),
                    prompt_sha256="b" * 64,
                    starting_state_sha256="c" * 64,
                    patch_sha256=f"{len(records) + 1:064x}",
                    study_config_sha256=config.digest(),
                    policy_sha256="d" * 64,
                    tool_profile=(
                        "core" if condition is ContextEfficiencyCondition.MSC_DELTA_CORE else "all"
                    ),
                    tool_schema_sha256="e" * 64,
                    dataset_sha256="f" * 64,
                    tokenizer_id="provider-fixture-v1",
                    counter_kind="exact",
                    counter_version="1.0.0",
                    tokenizer_sha256=tokenizer_evidence_sha256(
                        tokenizer_id="provider-fixture-v1",
                        counter_kind="exact",
                        counter_version="1.0.0",
                    ),
                    provider_token_attribution="exact",
                    provider_input_tokens=1000 if legacy else 600,
                    provider_output_tokens=100,
                    cost_usd=0.01,
                    latency_seconds=1.0,
                    agent_completed=True,
                    hidden_test_success=True,
                    execution_valid=True,
                    memory_context_text_tokens=400 if legacy else 120,
                    memory_delivery_payload_tokens=500 if legacy else 180,
                    memory_payload_overhead_tokens=100 if legacy else 60,
                    memory_evidence_tokens=0,
                    memory_history_tokens=0,
                    memory_delta_tokens=(
                        80 if condition is ContextEfficiencyCondition.MSC_DELTA else 0
                    ),
                    memory_full_equivalent_tokens=500,
                    other_memory_operation_llm_input_tokens=0,
                    other_memory_operation_llm_output_tokens=0,
                    other_memory_operation_token_attribution="exact_zero",
                    memory_tool_schema_tokens=200,
                    other_tool_schema_tokens=300,
                    constraint_loss=(
                        1
                        if unsafe and task_index == 0 and condition is config.activation_condition
                        else 0
                    ),
                    delta_threshold_results=(
                        tuple(
                            DeltaThresholdResult(
                                threshold=threshold,
                                policy_sha256=f"{index + 1:064x}",
                                patch_sha256=f"{task_index * 10 + index + 101:064x}",
                                execution_valid=True,
                                agent_completed=True,
                                hidden_test_success=True,
                                provider_input_tokens=900 if threshold == 0.5 else 650,
                                provider_output_tokens=100,
                                memory_delivery_payload_tokens=(500 if threshold == 0.5 else 180),
                                memory_delta_tokens=180,
                                memory_full_equivalent_tokens=500,
                                delta_hits=0 if threshold == 0.5 else 1,
                                full_fallbacks=1 if threshold == 0.5 else 0,
                            )
                            for index, threshold in enumerate(config.delta_thresholds)
                        )
                        if delta_condition
                        else ()
                    ),
                )
            )
    return records


def test_context_efficiency_study_does_not_mutate_existing_three_arm_enum() -> None:
    assert tuple(condition.value for condition in ExperimentCondition) == (
        "no_memory",
        "flat_memory",
        "memoryos",
    )
    assert len(ContextEfficiencyCondition) == 5


def test_config_hash_and_power_are_frozen_before_confirmatory_execution() -> None:
    config = _config()

    assert config.digest() == config.digest()
    assert config.required_sample_size() > 50
    assert config.condition_order(task_id="task-0", task_ordinal=0) != config.condition_order(
        task_id="task-1", task_ordinal=1
    )
    randomized = config.model_copy(
        update={"condition_ordering": "randomized", "condition_order_seed": 7}
    )
    assert randomized.condition_order(
        task_id="task-0", task_ordinal=0
    ) == randomized.condition_order(task_id="task-0", task_ordinal=99)
    with pytest.raises(ValueError, match="threshold grid"):
        ContextEfficiencyConfig(
            frozen_at=datetime(2026, 8, 15, tzinfo=UTC),
            delta_thresholds=(0.8,),
        )


@pytest.mark.v23
def test_dry_run_reports_token_and_safety_gates_but_never_activates() -> None:
    config = _config()
    started = datetime(2026, 8, 16, tzinfo=UTC)

    report = ContextEfficiencyStudyBuilder(config).build(
        _records(config),
        mode=ContextEfficiencyMode.DRY_RUN,
        run_id="fixture-dry-run",
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )

    assert report["protocol_valid"] is True
    assert report["effect_claim"] == "none"
    assert report["activation_approved"] is False
    assert report["default_mode_decision"] == "legacy"
    comparison = report["comparisons_to_legacy"][config.activation_condition.value]
    assert comparison["provider_input_tokens"]["difference"] == -400
    assert comparison["median_provider_input_reduction"] == 0.4
    assert comparison["accounting"]["provider_output_tokens"]["difference"] == 0
    assert comparison["accounting"]["memory_delivery_payload_tokens"]["difference"] == -320
    assert comparison["expected_provider_input_tokens_per_success"] == {
        "paired_n": 2,
        "baseline": 1000.0,
        "treatment": 600.0,
    }
    assert comparison["token_roi"]["status"] == "interpretable"
    assert comparison["token_roi"]["mean_tokens_saved"] == 400
    assert (
        report["condition_aggregates"]["msc_progressive"]["accounting"][
            "memory_tool_schema_tokens"
        ]["total"]
        == 400
    )
    threshold_sensitivity = report["delta_threshold_sensitivity"]["msc_delta"]
    assert threshold_sensitivity["0.5"]["runs"] == 2
    assert threshold_sensitivity["0.5"]["full_fallback_rate"] == 1.0
    assert threshold_sensitivity["0.65"]["delta_hit_rate"] == 1.0
    assert report["release_gates"]["success_noninferiority"]["power_qualified"] is False


@pytest.mark.v23
def test_confirmatory_fixture_and_safety_regression_cannot_make_effect_claim() -> None:
    config = _config()
    started = datetime(2026, 8, 16, tzinfo=UTC)

    report = ContextEfficiencyStudyBuilder(config).build(
        _records(config, unsafe=True),
        mode=ContextEfficiencyMode.CONFIRMATORY,
        run_id="fixture-confirmatory",
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )

    assert report["protocol_valid"] is False
    assert report["release_gates"]["safety"]["passed"] is False
    assert report["activation_approved"] is False
    assert report["default_mode_decision"] == "legacy"
    assert report["effect_claim"] == "none"
    assert "real_coding_agent" in " ".join(report["protocol_errors"])


def test_study_rejects_mixed_dataset_tokenizer_and_execution_evidence() -> None:
    config = _config()
    records = _records(config)
    records[1] = records[1].model_copy(
        update={
            "dataset_sha256": "0" * 64,
            "tokenizer_id": "different-tokenizer",
            "tokenizer_sha256": tokenizer_evidence_sha256(
                tokenizer_id="different-tokenizer",
                counter_kind="exact",
                counter_version="1.0.0",
            ),
            "execution_index": records[0].execution_index,
            "repository_id": "changed-across-conditions",
            "tool_schema_sha256": "1" * 64,
        }
    )
    started = datetime(2026, 8, 16, tzinfo=UTC)

    report = ContextEfficiencyStudyBuilder(config).build(
        records,
        mode=ContextEfficiencyMode.DRY_RUN,
        run_id="fixture-mixed-evidence",
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )

    errors = " ".join(report["protocol_errors"])
    assert report["protocol_valid"] is False
    assert "dataset hash" in errors
    assert "tokenizer identities" in errors
    assert "execution_index" in errors
    assert "task metadata" in errors
    assert "schema hashes" in errors


def test_study_record_rejects_inconsistent_token_attribution() -> None:
    config = _config()
    record = _records(config)[0]
    payload = record.model_dump(mode="json")

    with pytest.raises(ValueError, match="exact Provider attribution"):
        ContextEfficiencyRecord.model_validate(
            {
                **payload,
                "provider_input_tokens": None,
            }
        )
    with pytest.raises(ValueError, match="unavailable Provider attribution"):
        ContextEfficiencyRecord.model_validate(
            {
                **payload,
                "provider_token_attribution": "unavailable",
            }
        )
    with pytest.raises(ValueError, match="exact_zero other-memory attribution"):
        ContextEfficiencyRecord.model_validate(
            {
                **payload,
                "other_memory_operation_llm_input_tokens": None,
            }
        )


def test_context_efficiency_writer_is_reproducible_lf(tmp_path: Path) -> None:
    destination = tmp_path / "context-efficiency.json"

    write_context_efficiency_report(destination, {"value": "first\nsecond"})

    payload = destination.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r" not in payload


@pytest.mark.asyncio
@pytest.mark.v23
async def test_checked_in_dry_run_is_reproducible_and_covers_every_tool_profile() -> None:
    artifact = ROOT / "docs" / "verification" / "v2.3" / "context-efficiency-dry-run.json"

    rebuilt = await build_report(ROOT)
    checked_in = json.loads(artifact.read_text(encoding="utf-8"))

    assert rebuilt == checked_in
    assert set(rebuilt["schema_snapshots"]) == {"all", "core", "governance", "debug"}
    assert {record["condition"]: record["tool_profile"] for record in rebuilt["records"]} == {
        "legacy_full": "all",
        "msc_full": "all",
        "msc_progressive": "all",
        "msc_delta": "all",
        "msc_delta_core": "core",
    }
    assert rebuilt["effect_claim"] == "none"
    assert rebuilt["default_mode_decision"] == "legacy"
