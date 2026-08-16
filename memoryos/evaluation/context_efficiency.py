from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memoryos.context.token_meter import canonical_json
from memoryos.domain.schemas import MemoryOperationTokenAttribution

DELTA_THRESHOLD_GRID = (0.5, 0.65, 0.8, 0.9)
PROVIDER_BOOTSTRAP_SEED_OFFSET = 100
ACCOUNTING_BOOTSTRAP_SEED_OFFSET = 200
ACCOUNTING_BOOTSTRAP_CONDITION_STRIDE = 100
POWER_ESTIMATE_DECIMAL_PLACES = 12
REQUIRED_ADVERSARIAL_TAGS = (
    "negated_constraint",
    "numeric_threshold",
    "exception_condition",
    "truth_state_transition",
    "freshness_transition",
    "cross_repository_canary",
    "opposite_polarity",
    "evidence_moved",
)


class ContextEfficiencyCondition(StrEnum):
    LEGACY_FULL = "legacy_full"
    MSC_FULL = "msc_full"
    MSC_PROGRESSIVE = "msc_progressive"
    MSC_DELTA = "msc_delta"
    MSC_DELTA_CORE = "msc_delta_core"
    NO_MEMORY = "no_memory"
    MSC_CONTEXT_ONLY = "msc_context_only"


MEMORYOS_CONTEXT_CONDITIONS = (
    ContextEfficiencyCondition.LEGACY_FULL,
    ContextEfficiencyCondition.MSC_FULL,
    ContextEfficiencyCondition.MSC_PROGRESSIVE,
    ContextEfficiencyCondition.MSC_DELTA,
    ContextEfficiencyCondition.MSC_DELTA_CORE,
)


class ContextEfficiencyMode(StrEnum):
    DRY_RUN = "dry_run"
    CONFIRMATORY = "confirmatory"


class ProviderTokenAttribution(StrEnum):
    EXACT = "exact"
    UNAVAILABLE = "unavailable"


_OPTIONAL_ACCOUNTING_FIELDS = (
    "provider_input_tokens",
    "provider_output_tokens",
    "cached_input_tokens",
    "cost_usd",
    "memory_context_text_tokens",
    "memory_delivery_payload_tokens",
    "memory_payload_overhead_tokens",
    "memory_evidence_tokens",
    "memory_history_tokens",
    "memory_delta_tokens",
    "memory_full_equivalent_tokens",
    "other_memory_operation_llm_input_tokens",
    "other_memory_operation_llm_output_tokens",
    "memory_tool_schema_tokens",
    "other_tool_schema_tokens",
)
_REQUIRED_ACCOUNTING_FIELDS = (
    "latency_seconds",
    "context_compilation_llm_input_tokens",
    "context_compilation_llm_output_tokens",
    "memory_explain_calls",
    "memory_tool_calls",
)


class ContextEfficiencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    analysis_version: Literal["context-efficiency-v1"] = "context-efficiency-v1"
    frozen_at: datetime
    conditions: tuple[ContextEfficiencyCondition, ...] = (
        ContextEfficiencyCondition.LEGACY_FULL,
        ContextEfficiencyCondition.MSC_FULL,
        ContextEfficiencyCondition.MSC_PROGRESSIVE,
        ContextEfficiencyCondition.MSC_DELTA,
    )
    activation_condition: ContextEfficiencyCondition = ContextEfficiencyCondition.MSC_PROGRESSIVE
    condition_ordering: Literal["latin_square", "randomized"] = "latin_square"
    condition_order_seed: int = Field(default=20260815, ge=0)
    success_noninferiority_margin: float = Field(default=0.02, gt=0, le=0.2)
    token_median_reduction_minimum: float = Field(default=0.25, gt=0, lt=1)
    expected_paired_discordance_rate: float = Field(default=0.1, gt=0, le=1)
    target_power: float = Field(default=0.8, gt=0.5, lt=1)
    alpha_one_sided: float = Field(default=0.05, gt=0, lt=0.5)
    minimum_tasks: int = Field(default=50, ge=50)
    minimum_repositories: int = Field(default=3, ge=3)
    minimum_sequences: int = Field(default=10, ge=10)
    minimum_cross_step_tasks: int = Field(default=20, ge=20)
    delta_thresholds: tuple[float, ...] = DELTA_THRESHOLD_GRID
    bootstrap_rounds: int = Field(default=4000, ge=1000)
    bootstrap_seed: int = Field(default=20260815, ge=0)
    worst_group_minimum_n: int = Field(default=5, ge=2)

    @field_validator("frozen_at")
    @classmethod
    def require_aware_freeze_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("frozen_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("conditions")
    @classmethod
    def require_unique_conditions(
        cls,
        value: tuple[ContextEfficiencyCondition, ...],
    ) -> tuple[ContextEfficiencyCondition, ...]:
        if len(value) != len(set(value)) or len(value) < 4:
            raise ValueError("study conditions must be unique and include at least four arms")
        if value[0] is not ContextEfficiencyCondition.LEGACY_FULL:
            raise ValueError("legacy_full must remain the first study condition")
        return value

    @field_validator("delta_thresholds")
    @classmethod
    def require_frozen_delta_thresholds(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if value != DELTA_THRESHOLD_GRID:
            raise ValueError("the initial delta threshold grid is frozen at 0.5/0.65/0.8/0.9")
        return value

    @model_validator(mode="after")
    def require_activation_arm(self) -> ContextEfficiencyConfig:
        if self.activation_condition not in self.conditions:
            raise ValueError("activation_condition must be present in conditions")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    def required_sample_size(self) -> int:
        alpha_z = NormalDist().inv_cdf(1 - self.alpha_one_sided)
        power_z = NormalDist().inv_cdf(self.target_power)
        numerator = (alpha_z + power_z) * math.sqrt(self.expected_paired_discordance_rate)
        return math.ceil((numerator / self.success_noninferiority_margin) ** 2)

    def condition_order(
        self,
        *,
        task_id: str,
        task_ordinal: int,
    ) -> tuple[ContextEfficiencyCondition, ...]:
        ordered = list(self.conditions)
        if self.condition_ordering == "latin_square":
            shift = (task_ordinal + self.condition_order_seed) % len(ordered)
            return tuple((*ordered[shift:], *ordered[:shift]))
        seed = int.from_bytes(
            hashlib.sha256(f"{self.condition_order_seed}:{task_id}".encode()).digest()[:8],
            byteorder="big",
        )
        random.Random(seed).shuffle(ordered)  # noqa: S311 - preregistered experiment ordering
        return tuple(ordered)


class DeltaThresholdResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_valid: bool
    agent_completed: bool
    hidden_test_success: bool
    provider_input_tokens: int | None = Field(default=None, ge=0)
    provider_output_tokens: int | None = Field(default=None, ge=0)
    memory_delivery_payload_tokens: int = Field(ge=0)
    memory_delta_tokens: int = Field(ge=0)
    memory_full_equivalent_tokens: int = Field(ge=0)
    delta_hits: int = Field(default=0, ge=0)
    full_fallbacks: int = Field(default=0, ge=0)

    @field_validator("threshold")
    @classmethod
    def require_registered_threshold(cls, value: float) -> float:
        if value not in DELTA_THRESHOLD_GRID:
            raise ValueError("delta threshold must be one of 0.5/0.65/0.8/0.9")
        return value

    @property
    def functional_success(self) -> bool:
        return self.execution_valid and self.agent_completed and self.hidden_test_success


class ContextEfficiencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    repository_id: str
    sequence_id: str
    sequence_index: int = Field(ge=0)
    is_cross_step: bool
    intent: str
    agent_version: str
    model: str
    image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_type: Literal["real_coding_agent", "deterministic_fixture"]
    dataset_tier: Literal["public_replay", "private_authorized", "fixture"]
    condition: ContextEfficiencyCondition
    execution_index: int = Field(ge=0)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    starting_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    study_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_profile: Literal["none", "all", "core", "context", "governance", "debug"]
    tool_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_id: str
    counter_kind: Literal["exact", "estimated"]
    counter_version: str
    tokenizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_token_attribution: ProviderTokenAttribution
    provider_input_tokens: int | None = Field(default=None, ge=0)
    provider_output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_seconds: float = Field(ge=0)
    agent_completed: bool
    hidden_test_success: bool
    execution_valid: bool
    memory_context_text_tokens: int | None = Field(default=None, ge=0)
    memory_delivery_payload_tokens: int | None = Field(default=None, ge=0)
    memory_payload_overhead_tokens: int | None = Field(default=None, ge=0)
    memory_evidence_tokens: int | None = Field(default=None, ge=0)
    memory_history_tokens: int | None = Field(default=None, ge=0)
    memory_delta_tokens: int | None = Field(default=None, ge=0)
    memory_full_equivalent_tokens: int | None = Field(default=None, ge=0)
    context_compilation_llm_input_tokens: int = Field(default=0, ge=0)
    context_compilation_llm_output_tokens: int = Field(default=0, ge=0)
    other_memory_operation_llm_input_tokens: int | None = Field(default=None, ge=0)
    other_memory_operation_llm_output_tokens: int | None = Field(default=None, ge=0)
    other_memory_operation_token_attribution: MemoryOperationTokenAttribution = Field(
        default=MemoryOperationTokenAttribution.UNAVAILABLE
    )
    memory_tool_schema_tokens: int | None = Field(default=None, ge=0)
    other_tool_schema_tokens: int | None = Field(default=None, ge=0)
    memory_explain_calls: int = Field(default=0, ge=0)
    memory_tool_calls: int = Field(default=0, ge=0)
    constraint_loss: int = Field(default=0, ge=0)
    contested_bundle_split: int = Field(default=0, ge=0)
    stale_memory_uses: int = Field(default=0, ge=0)
    cross_project_leaks: int = Field(default=0, ge=0)
    facts_without_evidence: int = Field(default=0, ge=0)
    repeated_searches: int = Field(default=0, ge=0)
    repeated_file_opens: int = Field(default=0, ge=0)
    blocked_actions: int = Field(default=0, ge=0)
    context_rebases: int = Field(default=0, ge=0)
    delta_hits: int = Field(default=0, ge=0)
    full_fallbacks: int = Field(default=0, ge=0)
    snapshot_misses: int = Field(default=0, ge=0)
    contested: bool = False
    adversarial_tags: tuple[
        Literal[
            "negated_constraint",
            "numeric_threshold",
            "exception_condition",
            "truth_state_transition",
            "freshness_transition",
            "cross_repository_canary",
            "opposite_polarity",
            "evidence_moved",
        ],
        ...,
    ] = ()
    delta_threshold_results: tuple[DeltaThresholdResult, ...] = ()

    @model_validator(mode="after")
    def validate_delta_threshold_results(self) -> ContextEfficiencyRecord:
        values = [result.threshold for result in self.delta_threshold_results]
        if len(values) != len(set(values)):
            raise ValueError("delta threshold results must be unique")
        if len(self.adversarial_tags) != len(set(self.adversarial_tags)):
            raise ValueError("adversarial tags must be unique")
        expected_tokenizer_sha256 = tokenizer_evidence_sha256(
            tokenizer_id=self.tokenizer_id,
            counter_kind=self.counter_kind,
            counter_version=self.counter_version,
        )
        if self.tokenizer_sha256 != expected_tokenizer_sha256:
            raise ValueError("tokenizer_sha256 does not match tokenizer identity")
        delta_condition = self.condition in {
            ContextEfficiencyCondition.MSC_DELTA,
            ContextEfficiencyCondition.MSC_DELTA_CORE,
        }
        if self.delta_threshold_results and not delta_condition:
            raise ValueError("delta threshold results are only valid for delta conditions")
        other_values = (
            self.other_memory_operation_llm_input_tokens,
            self.other_memory_operation_llm_output_tokens,
        )
        if (
            self.other_memory_operation_token_attribution
            is MemoryOperationTokenAttribution.EXACT_ZERO
            and other_values != (0, 0)
        ):
            raise ValueError("exact_zero other-memory attribution requires two zero token values")
        if self.other_memory_operation_token_attribution in {
            MemoryOperationTokenAttribution.EXACT,
            MemoryOperationTokenAttribution.ESTIMATED,
        } and any(value is None for value in other_values):
            raise ValueError("attributed other-memory operations require complete token values")
        if (
            self.other_memory_operation_token_attribution
            is MemoryOperationTokenAttribution.UNAVAILABLE
            and any(value is not None for value in other_values)
        ):
            raise ValueError("unavailable other-memory attribution cannot carry token values")
        provider_values = (
            self.provider_input_tokens,
            self.provider_output_tokens,
            self.cached_input_tokens,
        )
        if self.provider_token_attribution is ProviderTokenAttribution.EXACT and any(
            value is None for value in provider_values[:2]
        ):
            raise ValueError("exact Provider attribution requires input and output token values")
        if self.provider_token_attribution is ProviderTokenAttribution.UNAVAILABLE and any(
            value is not None for value in provider_values
        ):
            raise ValueError("unavailable Provider attribution cannot carry token values")
        return self

    @property
    def functional_success(self) -> bool:
        return self.execution_valid and self.agent_completed and self.hidden_test_success


class ContextEfficiencyStudyBuilder:
    def __init__(self, config: ContextEfficiencyConfig) -> None:
        self.config = config

    def build(
        self,
        records: list[ContextEfficiencyRecord],
        *,
        mode: ContextEfficiencyMode,
        run_id: str,
        started_at: datetime,
        finished_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not records:
            raise ValueError("context efficiency study requires records")
        records = [
            ContextEfficiencyRecord.model_validate(record.model_dump(mode="json"))
            for record in records
        ]
        started = _aware_utc(started_at, field="started_at")
        finished = _aware_utc(finished_at or datetime.now(UTC), field="finished_at")
        if finished < started:
            raise ValueError("finished_at must not be earlier than started_at")
        indexed: dict[tuple[str, ContextEfficiencyCondition], ContextEfficiencyRecord] = {}
        for record in records:
            key = (record.task_id, record.condition)
            if key in indexed:
                raise ValueError(f"duplicate task/condition record: {key}")
            indexed[key] = record
        task_ids = sorted({record.task_id for record in records})
        protocol_errors = self._protocol_errors(indexed, task_ids, mode, started)
        comparisons = {
            condition.value: self._comparison(indexed, task_ids, condition)
            for condition in self.config.conditions
            if condition is not ContextEfficiencyCondition.LEGACY_FULL
        }
        activation = comparisons[self.config.activation_condition.value]
        protocol_valid = not protocol_errors
        gates = self._release_gates(activation, protocol_valid, mode)
        approved = all(bool(gate["passed"]) for gate in gates.values())
        effect_claim = (
            "minimum_sufficient_context_confirmed_on_preregistered_workload"
            if approved and mode is ContextEfficiencyMode.CONFIRMATORY
            else "none"
        )
        dataset_hashes = sorted({record.dataset_sha256 for record in records})
        report = {
            "artifact_encoding": "utf-8; newline=LF",
            "schema_version": "2.3",
            "study": "context_efficiency",
            "run_id": run_id,
            "mode": mode.value,
            "status": "completed" if protocol_valid else "completed_invalid",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "config": self.config.model_dump(mode="json"),
            "config_sha256": self.config.digest(),
            "dataset_sha256": dataset_hashes,
            "records_sha256": hashlib.sha256(
                canonical_json(
                    [record.model_dump(mode="json") for record in _ordered_records(records)]
                ).encode("utf-8")
            ).hexdigest(),
            "sample_size": len(task_ids),
            "condition_run_count": len(records),
            "protocol_valid": protocol_valid,
            "protocol_errors": protocol_errors,
            "power": self._power(len(task_ids)),
            "condition_aggregates": {
                condition.value: _condition_aggregate(
                    [record for record in records if record.condition is condition]
                )
                for condition in self.config.conditions
            },
            "delta_threshold_sensitivity": _delta_threshold_sensitivity(
                records,
                self.config.delta_thresholds,
            ),
            "comparisons_to_legacy": comparisons,
            "release_gates": gates,
            "activation_condition": self.config.activation_condition.value,
            "activation_approved": approved,
            "default_mode_decision": "msc" if approved else "legacy",
            "effect_claim": effect_claim,
            "records": [record.model_dump(mode="json") for record in _ordered_records(records)],
            "truthfulness": (
                "Dry-run and deterministic-fixture evidence cannot authorize activation."
                if mode is ContextEfficiencyMode.DRY_RUN
                else (
                    "Activation requires every preregistered gate and complete real-provider Usage."
                )
            ),
        }
        return report

    def _protocol_errors(
        self,
        indexed: dict[tuple[str, ContextEfficiencyCondition], ContextEfficiencyRecord],
        task_ids: list[str],
        mode: ContextEfficiencyMode,
        started_at: datetime,
    ) -> list[str]:
        errors: list[str] = []
        expected = set(self.config.conditions)
        config_hash = self.config.digest()
        for task_ordinal, task_id in enumerate(task_ids):
            task_records = {
                condition: indexed[(task_id, condition)]
                for condition in expected
                if (task_id, condition) in indexed
            }
            if set(task_records) != expected:
                errors.append(f"task {task_id} does not have every registered condition")
                continue
            records = list(task_records.values())
            observed_order = tuple(
                record.condition
                for record in sorted(records, key=lambda item: item.execution_index)
            )
            expected_order = self.config.condition_order(
                task_id=task_id,
                task_ordinal=task_ordinal,
            )
            if observed_order != expected_order:
                errors.append(f"task {task_id} did not follow the registered condition order")
            if len({record.prompt_sha256 for record in records}) != 1:
                errors.append(f"task {task_id} used non-identical prompts")
            if len({record.starting_state_sha256 for record in records}) != 1:
                errors.append(f"task {task_id} used non-identical starting state")
            task_identity = {
                (
                    record.repository_id,
                    record.sequence_id,
                    record.sequence_index,
                    record.is_cross_step,
                    record.intent,
                    record.evidence_type,
                    record.dataset_tier,
                    record.adversarial_tags,
                )
                for record in records
            }
            if len(task_identity) != 1:
                errors.append(f"task {task_id} changed task metadata across conditions")
            runtime_keys = {
                (
                    record.agent_version,
                    record.model,
                    record.image_digest,
                    record.runtime_sha256,
                )
                for record in records
            }
            if len(runtime_keys) != 1:
                errors.append(f"task {task_id} used non-identical agent runtime")
            if any(record.study_config_sha256 != config_hash for record in records):
                errors.append(f"task {task_id} did not use the frozen study config")
            for condition, record in task_records.items():
                expected_profile = (
                    "none"
                    if condition is ContextEfficiencyCondition.NO_MEMORY
                    else (
                        "context"
                        if condition is ContextEfficiencyCondition.MSC_CONTEXT_ONLY
                        else (
                            "core"
                            if condition is ContextEfficiencyCondition.MSC_DELTA_CORE
                            else "all"
                        )
                    )
                )
                if record.tool_profile != expected_profile:
                    errors.append(
                        f"task {task_id} condition {condition.value} used the wrong tool profile"
                    )
            if any(record.context_compilation_llm_input_tokens != 0 for record in records):
                errors.append(f"task {task_id} used LLM input tokens during context compilation")
            if any(record.context_compilation_llm_output_tokens != 0 for record in records):
                errors.append(f"task {task_id} used LLM output tokens during context compilation")
            if len({record.tokenizer_id for record in records}) != 1:
                errors.append(f"task {task_id} mixed tokenizer identities across conditions")
            if len({record.counter_kind for record in records}) != 1:
                errors.append(f"task {task_id} mixed counter kinds across conditions")
            if len({record.counter_version for record in records}) != 1:
                errors.append(f"task {task_id} mixed counter versions across conditions")
            if len({record.tokenizer_sha256 for record in records}) != 1:
                errors.append(f"task {task_id} mixed tokenizer hashes across conditions")
        all_records = list(indexed.values())
        if len({record.execution_index for record in all_records}) != len(all_records):
            errors.append("study execution_index values must be globally unique")
        if len({record.dataset_sha256 for record in all_records}) != 1:
            errors.append("study records must use one frozen dataset hash")
        tokenizer_evidence = {
            (
                record.tokenizer_id,
                record.counter_kind,
                record.counter_version,
                record.tokenizer_sha256,
            )
            for record in all_records
        }
        if len(tokenizer_evidence) != 1:
            errors.append("study records must use one frozen tokenizer identity")
        for profile in sorted({record.tool_profile for record in all_records}):
            profile_hashes = {
                record.tool_schema_sha256
                for record in all_records
                if record.tool_profile == profile
            }
            if len(profile_hashes) > 1:
                errors.append(f"tool profile {profile} mixed schema hashes")
        for condition in self.config.conditions:
            condition_records = [record for record in all_records if record.condition is condition]
            if len({record.policy_sha256 for record in condition_records}) > 1:
                errors.append(f"condition {condition.value} mixed policy hashes")
            if len({record.tool_schema_sha256 for record in condition_records}) > 1:
                errors.append(f"condition {condition.value} mixed tool schema hashes")
            for threshold in self.config.delta_thresholds:
                threshold_policy_hashes = {
                    result.policy_sha256
                    for record in condition_records
                    for result in record.delta_threshold_results
                    if result.threshold == threshold
                }
                if len(threshold_policy_hashes) > 1:
                    errors.append(
                        f"condition {condition.value} threshold {_threshold_key(threshold)} "
                        "mixed policy hashes"
                    )
        if mode is ContextEfficiencyMode.DRY_RUN:
            return sorted(set(errors))
        if self.config.frozen_at > started_at:
            errors.append("study configuration was not frozen before confirmatory execution")
        if len(task_ids) < self.config.minimum_tasks:
            errors.append(f"confirmatory study requires at least {self.config.minimum_tasks} tasks")
        repositories = {record.repository_id for record in indexed.values()}
        sequence_tasks: dict[str, set[str]] = {}
        for record in indexed.values():
            sequence_tasks.setdefault(record.sequence_id, set()).add(record.task_id)
        sequences = {
            sequence_id
            for sequence_id, sequence_task_ids in sequence_tasks.items()
            if len(sequence_task_ids) >= 2
        }
        cross_step_tasks = {record.task_id for record in indexed.values() if record.is_cross_step}
        if len(repositories) < self.config.minimum_repositories:
            errors.append("confirmatory study requires at least 3 repositories")
        if len(sequences) < self.config.minimum_sequences:
            errors.append("confirmatory study requires at least 10 sequences")
        if len(cross_step_tasks) < self.config.minimum_cross_step_tasks:
            errors.append("confirmatory study requires at least 20 cross-step tasks")
        observed_adversarial_tags = {
            tag for record in indexed.values() for tag in record.adversarial_tags
        }
        missing_adversarial_tags = set(REQUIRED_ADVERSARIAL_TAGS) - observed_adversarial_tags
        if missing_adversarial_tags:
            errors.append(
                "confirmatory study is missing adversarial coverage: "
                + ", ".join(sorted(missing_adversarial_tags))
            )
        if {record.contested for record in indexed.values()} != {False, True}:
            errors.append("confirmatory study requires contested and non-contested cohorts")
        if any(record.evidence_type != "real_coding_agent" for record in indexed.values()):
            errors.append("confirmatory study requires real_coding_agent evidence")
        if any(record.dataset_tier != "public_replay" for record in indexed.values()):
            errors.append("confirmatory study requires a public_replay dataset")
        if any(
            record.provider_token_attribution is not ProviderTokenAttribution.EXACT
            or record.provider_input_tokens is None
            or record.provider_output_tokens is None
            for record in indexed.values()
        ):
            errors.append("confirmatory token conclusions require exact Provider Usage")
        if any(record.cost_usd is None for record in all_records):
            errors.append("confirmatory study requires complete provider cost accounting")
        required_memory_fields = (
            "memory_context_text_tokens",
            "memory_delivery_payload_tokens",
            "memory_payload_overhead_tokens",
            "memory_evidence_tokens",
            "memory_history_tokens",
            "memory_delta_tokens",
            "memory_full_equivalent_tokens",
            "memory_tool_schema_tokens",
            "other_tool_schema_tokens",
        )
        if any(
            getattr(record, field) is None
            for record in all_records
            for field in required_memory_fields
        ):
            errors.append("confirmatory study requires complete memory and schema accounting")
        if any(
            record.other_memory_operation_token_attribution
            is MemoryOperationTokenAttribution.UNAVAILABLE
            for record in all_records
        ):
            errors.append("confirmatory study requires attributed other-memory model usage")
        registered_thresholds = set(self.config.delta_thresholds)
        for record in all_records:
            if record.condition not in {
                ContextEfficiencyCondition.MSC_DELTA,
                ContextEfficiencyCondition.MSC_DELTA_CORE,
            }:
                continue
            results = {result.threshold: result for result in record.delta_threshold_results}
            if set(results) != registered_thresholds:
                errors.append(
                    f"confirmatory {record.condition.value} requires every registered "
                    "delta threshold"
                )
                continue
            if any(
                result.provider_input_tokens is None or result.provider_output_tokens is None
                for result in results.values()
            ):
                errors.append(
                    f"confirmatory {record.condition.value} threshold sweep requires Provider Usage"
                )
        return sorted(set(errors))

    def _comparison(
        self,
        indexed: dict[tuple[str, ContextEfficiencyCondition], ContextEfficiencyRecord],
        task_ids: list[str],
        treatment: ContextEfficiencyCondition,
    ) -> dict[str, Any]:
        pairs = [
            (
                indexed[(task_id, ContextEfficiencyCondition.LEGACY_FULL)],
                indexed[(task_id, treatment)],
            )
            for task_id in task_ids
            if (task_id, ContextEfficiencyCondition.LEGACY_FULL) in indexed
            and (task_id, treatment) in indexed
        ]
        if not pairs:
            return {"paired_n": 0, "complete_provider_usage_n": 0}
        success = _paired_bootstrap(
            [float(left.functional_success) for left, _ in pairs],
            [float(right.functional_success) for _, right in pairs],
            rounds=self.config.bootstrap_rounds,
            seed=self.config.bootstrap_seed + list(self.config.conditions).index(treatment),
        )
        provider_pairs: list[tuple[int, int]] = []
        for left, right in pairs:
            left_tokens = left.provider_input_tokens
            right_tokens = right.provider_input_tokens
            if (
                left.provider_token_attribution is ProviderTokenAttribution.EXACT
                and right.provider_token_attribution is ProviderTokenAttribution.EXACT
                and left_tokens is not None
                and right_tokens is not None
            ):
                provider_pairs.append((left_tokens, right_tokens))
        provider_input = None
        median_reduction = None
        if provider_pairs:
            provider_input = _paired_bootstrap(
                [float(left) for left, _ in provider_pairs],
                [float(right) for _, right in provider_pairs],
                rounds=self.config.bootstrap_rounds,
                seed=(
                    self.config.bootstrap_seed
                    + PROVIDER_BOOTSTRAP_SEED_OFFSET
                    + list(self.config.conditions).index(treatment)
                ),
            )
            reductions = [
                (float(left) - float(right)) / float(left) for left, right in provider_pairs if left
            ]
            median_reduction = statistics.median(reductions) if reductions else None
        accounting: dict[str, Any] = {}
        for metric_index, field in enumerate(
            (*_OPTIONAL_ACCOUNTING_FIELDS, *_REQUIRED_ACCOUNTING_FIELDS)
        ):
            exact_provider = field in {
                "provider_input_tokens",
                "provider_output_tokens",
                "cached_input_tokens",
            }
            metric = _paired_field_metric(
                pairs,
                field,
                rounds=self.config.bootstrap_rounds,
                seed=(
                    self.config.bootstrap_seed
                    + ACCOUNTING_BOOTSTRAP_SEED_OFFSET
                    + list(self.config.conditions).index(treatment)
                    * ACCOUNTING_BOOTSTRAP_CONDITION_STRIDE
                    + metric_index
                ),
                require_exact_provider=exact_provider,
            )
            accounting[field] = metric
        if provider_input is not None:
            provider_input.update(
                {
                    "paired_n": len(provider_pairs),
                    "baseline_mean": statistics.fmean(left for left, _ in provider_pairs),
                    "treatment_mean": statistics.fmean(right for _, right in provider_pairs),
                }
            )
        safety = {
            "constraint_loss": sum(right.constraint_loss for _, right in pairs),
            "contested_bundle_split": sum(right.contested_bundle_split for _, right in pairs),
            "cross_project_leaks": sum(right.cross_project_leaks for _, right in pairs),
            "facts_without_evidence": sum(right.facts_without_evidence for _, right in pairs),
            "baseline_stale_memory_uses": sum(left.stale_memory_uses for left, _ in pairs),
            "treatment_stale_memory_uses": sum(right.stale_memory_uses for _, right in pairs),
        }
        transparency = {
            "provider_usage_complete": len(provider_pairs) == len(pairs),
            "study_config_hash_complete": all(
                left.study_config_sha256 == self.config.digest()
                and right.study_config_sha256 == self.config.digest()
                for left, right in pairs
            ),
            "policy_hash_complete": all(
                left.policy_sha256 and right.policy_sha256 for left, right in pairs
            ),
            "schema_hash_complete": all(
                left.tool_schema_sha256 and right.tool_schema_sha256 for left, right in pairs
            ),
            "cost_complete": all(
                left.cost_usd is not None and right.cost_usd is not None for left, right in pairs
            ),
            "memory_accounting_complete": all(
                getattr(record, field) is not None
                for pair in pairs
                for record in pair
                for field in (
                    "memory_context_text_tokens",
                    "memory_delivery_payload_tokens",
                    "memory_payload_overhead_tokens",
                    "memory_evidence_tokens",
                    "memory_history_tokens",
                    "memory_delta_tokens",
                    "memory_full_equivalent_tokens",
                    "memory_tool_schema_tokens",
                    "other_tool_schema_tokens",
                )
            ),
            "other_memory_operation_attribution_complete": all(
                record.other_memory_operation_token_attribution
                is not MemoryOperationTokenAttribution.UNAVAILABLE
                for pair in pairs
                for record in pair
            ),
            "tokenizers": sorted({record.tokenizer_id for pair in pairs for record in pair}),
            "counter_kinds": sorted({record.counter_kind for pair in pairs for record in pair}),
        }
        return {
            "paired_n": len(pairs),
            "complete_provider_usage_n": len(provider_pairs),
            "functional_success": success,
            "provider_input_tokens": provider_input,
            "median_provider_input_reduction": median_reduction,
            "accounting": accounting,
            "expected_provider_input_tokens_per_success": _expected_tokens_per_success(
                provider_pairs,
                [
                    (left.functional_success, right.functional_success)
                    for left, right in pairs
                    if left.provider_token_attribution is ProviderTokenAttribution.EXACT
                    and right.provider_token_attribution is ProviderTokenAttribution.EXACT
                    and left.provider_input_tokens is not None
                    and right.provider_input_tokens is not None
                ],
            ),
            "token_roi": _token_roi(
                provider_pairs,
                [
                    (left.functional_success, right.functional_success)
                    for left, right in pairs
                    if left.provider_token_attribution is ProviderTokenAttribution.EXACT
                    and right.provider_token_attribution is ProviderTokenAttribution.EXACT
                    and left.provider_input_tokens is not None
                    and right.provider_input_tokens is not None
                ],
            ),
            "safety": safety,
            "transparency": transparency,
            "system": {
                "delta_hits": sum(right.delta_hits for _, right in pairs),
                "full_fallbacks": sum(right.full_fallbacks for _, right in pairs),
                "snapshot_misses": sum(right.snapshot_misses for _, right in pairs),
                "delta_hit_rate": _rate(
                    sum(right.delta_hits for _, right in pairs),
                    sum(right.delta_hits + right.full_fallbacks for _, right in pairs),
                ),
                "full_fallback_rate": _rate(
                    sum(right.full_fallbacks for _, right in pairs),
                    sum(right.delta_hits + right.full_fallbacks for _, right in pairs),
                ),
            },
            "behavior": {
                "repeated_searches": sum(right.repeated_searches for _, right in pairs),
                "repeated_file_opens": sum(right.repeated_file_opens for _, right in pairs),
                "blocked_actions": sum(right.blocked_actions for _, right in pairs),
                "context_rebases": sum(right.context_rebases for _, right in pairs),
            },
            "worst_groups": self._worst_groups(pairs),
        }

    def _release_gates(
        self,
        comparison: dict[str, Any],
        protocol_valid: bool,
        mode: ContextEfficiencyMode,
    ) -> dict[str, dict[str, Any]]:
        success = comparison.get("functional_success") or {}
        provider = comparison.get("provider_input_tokens") or {}
        safety = comparison.get("safety") or {}
        transparency = comparison.get("transparency") or {}
        worst_groups = comparison.get("worst_groups") or {}
        confirmatory = mode is ContextEfficiencyMode.CONFIRMATORY
        power = self._power(int(comparison.get("paired_n", 0)))
        return {
            "protocol": {
                "passed": protocol_valid and confirmatory,
                "required": "valid preregistered confirmatory execution",
            },
            "success_noninferiority": {
                "passed": (
                    confirmatory
                    and power["qualified"]
                    and success.get("ci95_one_sided_low", -1.0)
                    > -self.config.success_noninferiority_margin
                ),
                "margin": self.config.success_noninferiority_margin,
                "power_qualified": power["qualified"],
            },
            "provider_input_tokens": {
                "passed": (
                    confirmatory
                    and provider.get("ci95_high", 1.0) < 0
                    and comparison.get("median_provider_input_reduction") is not None
                    and comparison["median_provider_input_reduction"]
                    >= self.config.token_median_reduction_minimum
                ),
                "median_reduction_minimum": self.config.token_median_reduction_minimum,
            },
            "safety": {
                "passed": (
                    safety.get("constraint_loss") == 0
                    and safety.get("contested_bundle_split") == 0
                    and safety.get("cross_project_leaks") == 0
                    and safety.get("facts_without_evidence") == 0
                    and safety.get("treatment_stale_memory_uses", 1)
                    <= safety.get("baseline_stale_memory_uses", 0)
                ),
                "required": "zero critical safety regressions",
            },
            "transparency": {
                "passed": bool(transparency)
                and all(
                    bool(transparency.get(field))
                    for field in (
                        "provider_usage_complete",
                        "study_config_hash_complete",
                        "policy_hash_complete",
                        "schema_hash_complete",
                        "cost_complete",
                        "memory_accounting_complete",
                        "other_memory_operation_attribution_complete",
                    )
                ),
                "required": "complete Provider Usage and reproducibility hashes",
            },
            "worst_groups": {
                "passed": (
                    set(worst_groups.get("evaluated_dimensions", []))
                    == {"repository", "intent", "agent_version", "contested"}
                    and not worst_groups.get("degraded_groups")
                    and not worst_groups.get("insufficient_groups")
                ),
                "minimum_group_n": self.config.worst_group_minimum_n,
            },
        }

    def _worst_groups(
        self,
        pairs: list[tuple[ContextEfficiencyRecord, ContextEfficiencyRecord]],
    ) -> dict[str, Any]:
        dimensions: dict[str, Callable[[ContextEfficiencyRecord], str]] = {
            "repository": lambda record: record.repository_id,
            "intent": lambda record: record.intent,
            "agent_version": lambda record: record.agent_version,
            "contested": lambda record: str(record.contested).lower(),
        }
        groups: list[dict[str, Any]] = []
        degraded: list[str] = []
        insufficient: list[str] = []
        for dimension, selector in dimensions.items():
            values = sorted({selector(right) for _, right in pairs})
            for value in values:
                selected = [(left, right) for left, right in pairs if selector(right) == value]
                if len(selected) < self.config.worst_group_minimum_n:
                    insufficient.append(f"{dimension}={value}")
                    continue
                difference = statistics.fmean(
                    float(right.functional_success) - float(left.functional_success)
                    for left, right in selected
                )
                constraint_loss = sum(right.constraint_loss for _, right in selected)
                contested_bundle_split = sum(right.contested_bundle_split for _, right in selected)
                cross_project_leaks = sum(right.cross_project_leaks for _, right in selected)
                facts_without_evidence = sum(right.facts_without_evidence for _, right in selected)
                baseline_stale_memory_uses = sum(left.stale_memory_uses for left, _ in selected)
                treatment_stale_memory_uses = sum(right.stale_memory_uses for _, right in selected)
                item = {
                    "dimension": dimension,
                    "value": value,
                    "paired_n": len(selected),
                    "functional_success_difference": difference,
                    "constraint_loss": constraint_loss,
                    "contested_bundle_split": contested_bundle_split,
                    "cross_project_leaks": cross_project_leaks,
                    "facts_without_evidence": facts_without_evidence,
                    "baseline_stale_memory_uses": baseline_stale_memory_uses,
                    "treatment_stale_memory_uses": treatment_stale_memory_uses,
                }
                groups.append(item)
                if (
                    difference < -self.config.success_noninferiority_margin
                    or constraint_loss > 0
                    or contested_bundle_split > 0
                    or cross_project_leaks > 0
                    or facts_without_evidence > 0
                    or treatment_stale_memory_uses > baseline_stale_memory_uses
                ):
                    degraded.append(f"{dimension}={value}")
        return {
            "groups": groups,
            "degraded_groups": sorted(degraded),
            "insufficient_groups": sorted(insufficient),
            "evaluated_dimensions": sorted(dimensions),
        }

    def _power(self, paired_n: int) -> dict[str, Any]:
        required = self.config.required_sample_size()
        alpha_z = NormalDist().inv_cdf(1 - self.config.alpha_one_sided)
        if paired_n <= 0:
            achieved = 0.0
        else:
            signal = self.config.success_noninferiority_margin * math.sqrt(
                paired_n / self.config.expected_paired_discordance_rate
            )
            achieved = NormalDist().cdf(signal - alpha_z)
        stable_achieved = round(achieved, POWER_ESTIMATE_DECIMAL_PLACES)
        return {
            "paired_n": paired_n,
            "required_n": required,
            "target": self.config.target_power,
            "estimated_achieved": stable_achieved,
            "qualified": (paired_n >= required and stable_achieved >= self.config.target_power),
            "assumption": {
                "paired_discordance_rate": self.config.expected_paired_discordance_rate,
                "noninferiority_margin": self.config.success_noninferiority_margin,
                "alpha_one_sided": self.config.alpha_one_sided,
            },
        }


def _paired_field_metric(
    pairs: list[tuple[ContextEfficiencyRecord, ContextEfficiencyRecord]],
    field: str,
    *,
    rounds: int,
    seed: int,
    require_exact_provider: bool,
) -> dict[str, Any]:
    complete: list[tuple[float, float]] = []
    for left, right in pairs:
        if require_exact_provider and (
            left.provider_token_attribution is not ProviderTokenAttribution.EXACT
            or right.provider_token_attribution is not ProviderTokenAttribution.EXACT
        ):
            continue
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value is None or right_value is None:
            continue
        complete.append((float(left_value), float(right_value)))
    if not complete:
        return {
            "paired_n": 0,
            "baseline_mean": None,
            "treatment_mean": None,
            "difference": None,
            "ci95_low": None,
            "ci95_high": None,
        }
    metric = _paired_bootstrap(
        [left for left, _ in complete],
        [right for _, right in complete],
        rounds=rounds,
        seed=seed,
    )
    metric.update(
        {
            "paired_n": len(complete),
            "baseline_mean": statistics.fmean(left for left, _ in complete),
            "treatment_mean": statistics.fmean(right for _, right in complete),
        }
    )
    return metric


def _condition_aggregate(records: list[ContextEfficiencyRecord]) -> dict[str, Any]:
    if not records:
        return {"runs": 0}
    accounting: dict[str, Any] = {}
    for field in (*_OPTIONAL_ACCOUNTING_FIELDS, *_REQUIRED_ACCOUNTING_FIELDS):
        values = [getattr(record, field) for record in records]
        complete = [float(value) for value in values if value is not None]
        accounting[field] = {
            "complete_n": len(complete),
            "mean": statistics.fmean(complete) if complete else None,
            "total": sum(complete) if complete else None,
        }
    return {
        "runs": len(records),
        "functional_success_rate": statistics.fmean(
            float(record.functional_success) for record in records
        ),
        "accounting": accounting,
        "safety": {
            "constraint_loss": sum(record.constraint_loss for record in records),
            "contested_bundle_split": sum(record.contested_bundle_split for record in records),
            "stale_memory_uses": sum(record.stale_memory_uses for record in records),
            "cross_project_leaks": sum(record.cross_project_leaks for record in records),
            "facts_without_evidence": sum(record.facts_without_evidence for record in records),
        },
        "behavior": {
            "repeated_searches": sum(record.repeated_searches for record in records),
            "repeated_file_opens": sum(record.repeated_file_opens for record in records),
            "blocked_actions": sum(record.blocked_actions for record in records),
            "context_rebases": sum(record.context_rebases for record in records),
        },
        "system": {
            "delta_hits": sum(record.delta_hits for record in records),
            "full_fallbacks": sum(record.full_fallbacks for record in records),
            "snapshot_misses": sum(record.snapshot_misses for record in records),
        },
        "attribution": {
            "provider_token_attribution": sorted(
                {record.provider_token_attribution.value for record in records}
            ),
            "tokenizer_ids": sorted({record.tokenizer_id for record in records}),
            "counter_kinds": sorted({record.counter_kind for record in records}),
            "counter_versions": sorted({record.counter_version for record in records}),
            "tokenizer_sha256": sorted({record.tokenizer_sha256 for record in records}),
            "tool_profiles": sorted({record.tool_profile for record in records}),
            "other_memory_operation_token_attribution": sorted(
                {record.other_memory_operation_token_attribution.value for record in records}
            ),
            "policy_sha256": sorted({record.policy_sha256 for record in records}),
            "tool_schema_sha256": sorted({record.tool_schema_sha256 for record in records}),
            "dataset_sha256": sorted({record.dataset_sha256 for record in records}),
            "dataset_tiers": sorted({record.dataset_tier for record in records}),
            "runtime_sha256": sorted({record.runtime_sha256 for record in records}),
        },
    }


def _delta_threshold_sensitivity(
    records: list[ContextEfficiencyRecord],
    thresholds: tuple[float, ...],
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for condition in (
        ContextEfficiencyCondition.MSC_DELTA,
        ContextEfficiencyCondition.MSC_DELTA_CORE,
    ):
        condition_records = [record for record in records if record.condition is condition]
        if not condition_records:
            continue
        threshold_report: dict[str, Any] = {}
        for threshold in thresholds:
            results = [
                result
                for record in condition_records
                for result in record.delta_threshold_results
                if result.threshold == threshold
            ]
            provider_inputs = [
                result.provider_input_tokens
                for result in results
                if result.provider_input_tokens is not None
            ]
            provider_outputs = [
                result.provider_output_tokens
                for result in results
                if result.provider_output_tokens is not None
            ]
            delta_hits = sum(result.delta_hits for result in results)
            full_fallbacks = sum(result.full_fallbacks for result in results)
            threshold_report[_threshold_key(threshold)] = {
                "runs": len(results),
                "functional_success_rate": (
                    statistics.fmean(float(result.functional_success) for result in results)
                    if results
                    else None
                ),
                "provider_input_tokens": {
                    "complete_n": len(provider_inputs),
                    "mean": statistics.fmean(provider_inputs) if provider_inputs else None,
                },
                "provider_output_tokens": {
                    "complete_n": len(provider_outputs),
                    "mean": statistics.fmean(provider_outputs) if provider_outputs else None,
                },
                "memory_delivery_payload_tokens": _result_metric(
                    results, "memory_delivery_payload_tokens"
                ),
                "memory_delta_tokens": _result_metric(results, "memory_delta_tokens"),
                "memory_full_equivalent_tokens": _result_metric(
                    results, "memory_full_equivalent_tokens"
                ),
                "delta_hits": delta_hits,
                "full_fallbacks": full_fallbacks,
                "delta_hit_rate": _rate(delta_hits, delta_hits + full_fallbacks),
                "full_fallback_rate": _rate(full_fallbacks, delta_hits + full_fallbacks),
                "policy_sha256": sorted({result.policy_sha256 for result in results}),
            }
        report[condition.value] = threshold_report
    return report


def _result_metric(results: list[DeltaThresholdResult], field: str) -> dict[str, Any]:
    values = [float(getattr(result, field)) for result in results]
    return {
        "total": sum(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
    }


def _threshold_key(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _expected_tokens_per_success(
    provider_pairs: list[tuple[int, int]],
    outcomes: list[tuple[bool, bool]],
) -> dict[str, float | int | None]:
    if not provider_pairs or len(provider_pairs) != len(outcomes):
        return {"paired_n": 0, "baseline": None, "treatment": None}
    baseline_success = statistics.fmean(float(left) for left, _ in outcomes)
    treatment_success = statistics.fmean(float(right) for _, right in outcomes)
    baseline_tokens = statistics.fmean(float(left) for left, _ in provider_pairs)
    treatment_tokens = statistics.fmean(float(right) for _, right in provider_pairs)
    return {
        "paired_n": len(provider_pairs),
        "baseline": baseline_tokens / baseline_success if baseline_success > 0 else None,
        "treatment": treatment_tokens / treatment_success if treatment_success > 0 else None,
    }


def _token_roi(
    provider_pairs: list[tuple[int, int]],
    outcomes: list[tuple[bool, bool]],
) -> dict[str, Any]:
    if not provider_pairs or len(provider_pairs) != len(outcomes):
        return {
            "status": "unavailable",
            "paired_n": 0,
            "success_delta": None,
            "mean_tokens_saved": None,
            "success_delta_per_token_saved": None,
        }
    success_delta = statistics.fmean(float(right) - float(left) for left, right in outcomes)
    mean_tokens_saved = statistics.fmean(
        float(left) - float(right) for left, right in provider_pairs
    )
    interpretable = success_delta >= 0 and mean_tokens_saved > 0
    return {
        "status": "interpretable" if interpretable else "no_positive_roi",
        "paired_n": len(provider_pairs),
        "success_delta": success_delta,
        "mean_tokens_saved": mean_tokens_saved,
        "success_delta_per_token_saved": (
            success_delta / mean_tokens_saved if interpretable else None
        ),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def tokenizer_evidence_sha256(
    *,
    tokenizer_id: str,
    counter_kind: str,
    counter_version: str,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "tokenizer_id": tokenizer_id,
                "counter_kind": counter_kind,
                "counter_version": counter_version,
            }
        ).encode("utf-8")
    ).hexdigest()


def write_context_efficiency_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _paired_bootstrap(
    baseline: list[float],
    treatment: list[float],
    *,
    rounds: int,
    seed: int,
) -> dict[str, float]:
    if not baseline or len(baseline) != len(treatment):
        raise ValueError("paired bootstrap requires equal non-empty vectors")
    differences = [right - left for left, right in zip(baseline, treatment, strict=True)]
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling
    samples = []
    for _ in range(rounds):
        samples.append(
            statistics.fmean(differences[rng.randrange(len(differences))] for _ in differences)
        )
    samples.sort()
    return {
        "difference": statistics.fmean(differences),
        "ci95_low": samples[int(rounds * 0.025)],
        "ci95_high": samples[min(rounds - 1, int(rounds * 0.975))],
        "ci95_one_sided_low": samples[int(rounds * 0.05)],
    }


def _ordered_records(
    records: list[ContextEfficiencyRecord],
) -> list[ContextEfficiencyRecord]:
    return sorted(records, key=lambda item: (item.task_id, item.condition.value))


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "ACCOUNTING_BOOTSTRAP_CONDITION_STRIDE",
    "ACCOUNTING_BOOTSTRAP_SEED_OFFSET",
    "DELTA_THRESHOLD_GRID",
    "MEMORYOS_CONTEXT_CONDITIONS",
    "PROVIDER_BOOTSTRAP_SEED_OFFSET",
    "REQUIRED_ADVERSARIAL_TAGS",
    "ContextEfficiencyCondition",
    "ContextEfficiencyConfig",
    "ContextEfficiencyMode",
    "ContextEfficiencyRecord",
    "ContextEfficiencyStudyBuilder",
    "DeltaThresholdResult",
    "ProviderTokenAttribution",
    "tokenizer_evidence_sha256",
    "write_context_efficiency_report",
]
