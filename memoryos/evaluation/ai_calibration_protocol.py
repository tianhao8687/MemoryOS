from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from memoryos.evaluation.ai_jury import AIJuryProtocol
from memoryos.evaluation.evidence_hashing import canonical_file_sha256
from memoryos.evaluation.retrieval_calibration_features import (
    default_weight_training_protocol,
)
from memoryos.evaluation.retrieval_weight_calibration import (
    WeightPromotionProtocol,
    WeightTrainingProtocol,
    weight_training_protocol_digest,
)


class AIOnlyCalibrationProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Literal["memoryos-ai-executable-calibration-v1"] = (
        "memoryos-ai-executable-calibration-v1"
    )
    status: Literal["protocol_ready_evidence_pending"] = "protocol_ready_evidence_pending"
    human_review_required: Literal[False] = False
    human_gold_claim: Literal[False] = False
    production_weights_frozen: Literal[True] = True
    activation_requires_promotion_decision: Literal[True] = True
    ai_jury: AIJuryProtocol
    weight_training: WeightTrainingProtocol
    promotion: WeightPromotionProtocol
    requirements: list[str]

    @model_validator(mode="after")
    def validate_closed_loop(self) -> AIOnlyCalibrationProtocol:
        if self.ai_jury.min_model_families < 3:
            raise ValueError("AI-only calibration requires at least three model families")
        if self.ai_jury.min_providers < 3:
            raise ValueError("AI-only calibration requires at least three model providers")
        if not self.weight_training.hard_gate_features:
            raise ValueError("AI-only calibration must name non-learned hard safety gates")
        if self.promotion.min_repositories < 3:
            raise ValueError("promotion requires repository-held-out evidence")
        if self.promotion.expected_training_protocol_sha256 != weight_training_protocol_digest(
            self.weight_training
        ):
            raise ValueError("promotion must bind the frozen weight-training protocol")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class CalibrationEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=500)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: Literal["ai_weak_supervision", "fixture_plumbing", "real_agent_ablation"]
    records: int = Field(ge=0)
    effective_model_families: int = Field(ge=0)
    effective_providers: int = Field(ge=0)
    real_agent_tasks: int = Field(ge=0)
    executable_ablation_pairs: int = Field(ge=0)
    candidate_training_eligible: Literal[False] = False
    promotion_eligible: Literal[False] = False
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def require_safe_relative_path(self) -> CalibrationEvidenceItem:
        artifact_path = Path(self.path)
        if artifact_path.is_absolute() or ".." in artifact_path.parts:
            raise ValueError("evidence paths must be repository-relative and non-traversing")
        return self


class CalibrationGateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_jury_model_families: int = Field(ge=0)
    effective_jury_providers: int = Field(ge=0)
    order_swapped_pairwise_comparisons: int = Field(ge=0)
    real_agent_ablation_pairs: int = Field(ge=0)
    sealed_promotion_tasks: int = Field(ge=0)
    sealed_promotion_repositories: int = Field(ge=0)
    sealed_promotion_sequences: int = Field(ge=0)
    distinct_promotion_agent_models: int = Field(ge=0)
    complete_cost_pairs: int = Field(ge=0)
    candidate_profile_available: bool
    promotion_approved: bool


class AICalibrationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["protocol_ready_evidence_pending"] = "protocol_ready_evidence_pending"
    protocol_id: Literal["memoryos-ai-executable-calibration-v1"] = (
        "memoryos-ai-executable-calibration-v1"
    )
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_review_required: Literal[False] = False
    production_weights_frozen: Literal[True] = True
    production_profile_active: Literal[False] = False
    evidence: list[CalibrationEvidenceItem]
    gates: CalibrationGateSnapshot
    blockers: list[str] = Field(min_length=1)


def default_ai_calibration_protocol() -> AIOnlyCalibrationProtocol:
    weight_training = default_weight_training_protocol()
    return AIOnlyCalibrationProtocol(
        ai_jury=AIJuryProtocol(),
        weight_training=weight_training,
        promotion=WeightPromotionProtocol(
            min_agent_models=2,
            expected_training_protocol_sha256=weight_training_protocol_digest(weight_training),
        ),
        requirements=[
            "Pairwise AI judgments must be repeated in both presentation orders.",
            "Votes from one model family contribute at most one family-level vote.",
            "AI-jury labels remain probabilistic weak supervision.",
            "Uncalibrated jury thresholds remain explicit provisional policy defaults.",
            "Only discordant, protocol-valid executable ablations create causal labels.",
            "Safety, temporal, scope, privacy, and archive exclusions remain hard gates.",
            "Training and development repositories must be disjoint.",
            "Sealed promotion observations must never enter candidate training or dev selection.",
            "Candidate weights may run only through an explicit shadow scoring projection.",
            "Frozen structural parameters remain provisional until sealed end-to-end promotion.",
            "Promotion requires sealed real-agent outcomes from unseen agent models.",
            "An approved profile is never activated implicitly.",
        ],
    )


def load_ai_calibration_protocol(path: Path) -> AIOnlyCalibrationProtocol:
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 AI calibration protocol: {path}") from exc
    return TypeAdapter(AIOnlyCalibrationProtocol).validate_python(payload)


def load_ai_calibration_readiness(path: Path) -> AICalibrationReadiness:
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 AI calibration readiness artifact: {path}") from exc
    return TypeAdapter(AICalibrationReadiness).validate_python(payload)


def validate_ai_calibration_assets(
    repository_root: Path,
    *,
    protocol_path: Path | None = None,
    readiness_path: Path | None = None,
) -> AICalibrationReadiness:
    root = repository_root.resolve()
    protocol_file = protocol_path or root / "benchmarks/ai_calibration_v1/protocol.json"
    readiness_file = readiness_path or root / "benchmarks/ai_calibration_v1/readiness.json"
    protocol = load_ai_calibration_protocol(protocol_file)
    readiness = load_ai_calibration_readiness(readiness_file)
    if protocol != default_ai_calibration_protocol():
        raise ValueError("checked-in AI calibration protocol differs from the frozen default")
    if readiness.protocol_sha256 != protocol.digest():
        raise ValueError("readiness protocol digest does not match the frozen protocol")
    if readiness.protocol_file_sha256 != _file_sha256(protocol_file):
        raise ValueError("readiness protocol file hash is stale")
    for artifact in readiness.evidence:
        artifact_path = (root / artifact.path).resolve()
        if not artifact_path.is_relative_to(root):
            raise ValueError(f"evidence artifact escapes repository root: {artifact.path}")
        if not artifact_path.is_file():
            raise ValueError(f"evidence artifact is missing: {artifact.path}")
        if _file_sha256(artifact_path) != artifact.file_sha256:
            raise ValueError(f"evidence artifact hash is stale: {artifact.path}")
        _validate_evidence_semantics(artifact, artifact_path, root)
    if readiness.gates.effective_jury_model_families != max(
        (item.effective_model_families for item in readiness.evidence),
        default=0,
    ):
        raise ValueError("readiness jury-family count differs from its evidence inventory")
    if readiness.gates.effective_jury_providers != max(
        (item.effective_providers for item in readiness.evidence),
        default=0,
    ):
        raise ValueError("readiness jury-provider count differs from its evidence inventory")
    if readiness.gates.real_agent_ablation_pairs != sum(
        item.executable_ablation_pairs for item in readiness.evidence
    ):
        raise ValueError("readiness real-ablation count differs from its evidence inventory")
    return readiness


def _file_sha256(path: Path) -> str:
    return canonical_file_sha256(path)


def _validate_evidence_semantics(
    artifact: CalibrationEvidenceItem,
    artifact_path: Path,
    repository_root: Path,
) -> None:
    try:
        payload = json.loads(artifact_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"evidence artifact is not valid UTF-8 JSON: {artifact.path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"evidence artifact must contain a JSON object: {artifact.path}")
    if artifact.artifact_id == "model-review-provisional-v1":
        distribution = payload.get("adjudicated_distribution")
        if not isinstance(distribution, dict) or distribution.get("rows") != artifact.records:
            raise ValueError("model-review readiness row count is stale")
        if payload.get("human_gold_claim") is not False:
            raise ValueError("model-review evidence must not claim human gold")
        if payload.get("label_tier") != "model_adjudicated_provisional":
            raise ValueError("model-review evidence tier changed")
    elif artifact.artifact_id == "markupsafe-deterministic-public-smoke":
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            raise ValueError("fixture readiness evidence is missing runtime metadata")
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != artifact.records:
            raise ValueError("fixture readiness record count is stale")
        if runtime.get("evidence_type") != "deterministic_fixture":
            raise ValueError("fixture readiness evidence unexpectedly used a real agent")
        if payload.get("effect_claim") != "none":
            raise ValueError("fixture readiness evidence must not claim an effect")
    elif artifact.artifact_id in {
        "requests-6028-real-agent-ablation-v1",
        "swebench-cross-repository-real-agent-ablation-v1",
        "swebench-label-seeking-real-agent-ablation-v1-v2",
    }:
        _validate_real_agent_ablation_evidence(artifact, payload, repository_root)
    else:
        raise ValueError(f"unknown checked-in evidence artifact: {artifact.artifact_id}")


def _validate_real_agent_ablation_evidence(
    artifact: CalibrationEvidenceItem,
    payload: dict[str, object],
    repository_root: Path,
) -> None:
    if artifact.artifact_id == "requests-6028-real-agent-ablation-v1":
        _validate_pinned_evidence_file(payload.get("manifest"), repository_root)
    elif artifact.artifact_id == "swebench-label-seeking-real-agent-ablation-v1-v2":
        task_packs = payload.get("task_packs")
        if not isinstance(task_packs, list) or len(task_packs) != 2:
            raise ValueError("label-seeking evidence must bind both frozen task packs")
        audits: list[dict[str, object]] = []
        pack_ids: set[str] = set()
        for task_pack in task_packs:
            if not isinstance(task_pack, dict):
                raise ValueError("label-seeking task-pack binding must be an object")
            pack_id = task_pack.get("id")
            if not isinstance(pack_id, str) or not pack_id or pack_id in pack_ids:
                raise ValueError("label-seeking task-pack IDs must be unique")
            pack_ids.add(pack_id)
            for key in (
                "manifest",
                "partition_lock",
                "provenance",
                "run_lock",
                "scorer_verification",
                "post_run_audit",
            ):
                _validate_pinned_evidence_file(task_pack.get(key), repository_root)
            audits.append(
                _load_pinned_evidence_json(task_pack.get("post_run_audit"), repository_root)
            )
        if pack_ids != {"label-seek-v1", "label-seek-v2"}:
            raise ValueError("label-seeking evidence bound unexpected task packs")
        expected_audit_summary = {
            "protocol_valid_pairs": sum(
                _audit_count(audit.get("protocol_valid_pairs"), "protocol-valid pairs")
                for audit in audits
            ),
            "invalid_pairs": sum(
                _audit_count(
                    audit.get("invalidated_pairs", audit.get("invalid_pairs")),
                    "invalid pairs",
                )
                for audit in audits
            ),
            "eligible_training_observations": sum(
                _audit_count(
                    audit.get("eligible_training_observations"),
                    "eligible training observations",
                )
                for audit in audits
            ),
        }
        if payload.get("audit_summary") != expected_audit_summary:
            raise ValueError("label-seeking evidence audit summary is stale")
        if expected_audit_summary["protocol_valid_pairs"] != artifact.executable_ablation_pairs:
            raise ValueError("label-seeking audit valid-pair count differs from readiness")
        if expected_audit_summary["eligible_training_observations"] != 0:
            raise ValueError("label-seeking audit unexpectedly contains an eligible label")
    else:
        task_pack = payload.get("task_pack")
        if not isinstance(task_pack, dict):
            raise ValueError("cross-repository evidence is missing its task-pack bindings")
        for key in ("manifest", "partition_lock", "provenance", "scorer_verification"):
            _validate_pinned_evidence_file(task_pack.get(key), repository_root)

    runtime = payload.get("runtime")
    pairs = payload.get("pairs")
    aggregate = payload.get("aggregate")
    training = payload.get("training_observations", payload.get("training_observation"))
    if not isinstance(runtime, dict) or runtime.get("evidence_type") != "real_coding_agent":
        raise ValueError("real-agent ablation evidence has invalid runtime metadata")
    if not isinstance(pairs, list) or len(pairs) != artifact.executable_ablation_pairs:
        raise ValueError("real-agent ablation pair count is stale")
    if artifact.records != 2 * len(pairs):
        raise ValueError("real-agent ablation record count is stale")

    task_ids: set[str] = set()
    discordant_pairs = 0
    helped_pairs = 0
    harmed_pairs = 0
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("real-agent ablation pair must be an object")
        pair_task = pair.get("task_id")
        if pair_task is not None:
            if not isinstance(pair_task, str) or not pair_task:
                raise ValueError("real-agent ablation pair has an invalid task ID")
            task_ids.add(pair_task)
        partition = pair.get("partition")
        if partition is not None and partition not in {"train", "dev"}:
            raise ValueError("published real-agent ablation pair has an invalid partition")
        scorer_hash = pair.get("scorer_sha256")
        if scorer_hash is not None and not _is_sha256(scorer_hash):
            raise ValueError("published real-agent ablation pair has an invalid scorer hash")
        full = pair.get("full")
        minus = pair.get("minus")
        if not isinstance(full, dict) or not isinstance(minus, dict):
            raise ValueError("real-agent ablation pair is missing an arm")
        if full.get("protocol_valid") is not True or minus.get("protocol_valid") is not True:
            raise ValueError("published real-agent ablation arms must be protocol-valid")
        if full.get("target_memory_selected") is not True:
            raise ValueError("published full arm did not select the target memory")
        if minus.get("target_memory_selected") is not False:
            raise ValueError("published minus arm selected the excluded target memory")
        if full.get("prompt_sha256") != minus.get("prompt_sha256"):
            raise ValueError("published real-agent ablation pair changed its prompt")
        if full.get("runtime_sha256") != minus.get("runtime_sha256"):
            raise ValueError("published real-agent ablation pair changed its runtime")
        for arm in (full, minus):
            for field in (
                "patch_sha256",
                "prompt_sha256",
                "runtime_sha256",
                "source_report_sha256",
            ):
                if not _is_sha256(arm.get(field)):
                    raise ValueError(f"published real-agent arm has an invalid {field}")
            if arm.get("cross_project_leaks", 0) != 0 or arm.get("stale_memory_uses", 0) != 0:
                raise ValueError("published real-agent ablation arm failed a safety gate")
        full_success = full.get("hidden_test_success")
        minus_success = minus.get("hidden_test_success")
        if not isinstance(full_success, bool) or not isinstance(minus_success, bool):
            raise ValueError("published real-agent ablation arm omitted its outcome")
        discordant = full_success != minus_success
        if pair.get("discordant_success") is not discordant:
            raise ValueError("published real-agent ablation discordance flag is stale")
        discordant_pairs += int(discordant)
        helped_pairs += int(full_success and not minus_success)
        harmed_pairs += int(minus_success and not full_success)

    if task_ids:
        if len(task_ids) != artifact.real_agent_tasks:
            raise ValueError("real-agent ablation task count is stale")
    elif artifact.real_agent_tasks != 1:
        raise ValueError("single-task real-agent evidence has a stale task count")
    if not isinstance(aggregate, dict) or aggregate.get("safety_worsened_pairs") != 0:
        raise ValueError("real-agent ablation aggregate failed its safety gate")
    if aggregate.get("attempted_pairs") != len(pairs):
        raise ValueError("real-agent ablation aggregate attempted-pair count is stale")
    if aggregate.get("valid_pairs") != len(pairs):
        raise ValueError("real-agent ablation aggregate valid-pair count is stale")
    if aggregate.get("helped_pairs") != helped_pairs:
        raise ValueError("real-agent ablation aggregate helped-pair count is stale")
    if aggregate.get("harmed_pairs") != harmed_pairs:
        raise ValueError("real-agent ablation aggregate harmed-pair count is stale")
    if aggregate.get("unchanged_pairs") != len(pairs) - discordant_pairs:
        raise ValueError("real-agent ablation aggregate unchanged-pair count is stale")
    if aggregate.get("production_eligible") is not False:
        raise ValueError("real-agent ablation evidence cannot be promotion evidence")
    if not isinstance(training, dict):
        raise ValueError("real-agent training-observation summary is missing")
    if training.get("real_executable_labels") != discordant_pairs:
        raise ValueError("real-agent training-observation count is stale")

    invalidated = payload.get("invalidated_attempts", [])
    if not isinstance(invalidated, list):
        raise ValueError("real-agent invalidated-attempt register must be a list")
    for attempt in invalidated:
        if not isinstance(attempt, dict):
            raise ValueError("real-agent invalidated attempt must be an object")
        if attempt.get("excluded_from_counts") is not True:
            raise ValueError("invalidated real-agent attempt was not excluded from counts")
        if attempt.get("reason") != "scorer_invalid":
            raise ValueError("real-agent attempt has an unsupported invalidation reason")
        old_hash = attempt.get("old_scorer_sha256")
        corrected_hash = attempt.get("corrected_scorer_sha256")
        if not _is_sha256(old_hash) or not _is_sha256(corrected_hash) or old_hash == corrected_hash:
            raise ValueError("real-agent scorer invalidation hashes are malformed")
        checks = attempt.get("corrected_scorer_checks")
        if not isinstance(checks, dict):
            raise ValueError("real-agent scorer invalidation lacks corrected checks")
        if checks.get("base_exit_code") == 0 or checks.get("solution_exit_code") != 0:
            raise ValueError("corrected scorer does not separate the base and solution")
        captured_exits = checks.get("captured_arm_exit_codes")
        if not isinstance(captured_exits, dict) or not captured_exits:
            raise ValueError("scorer invalidation lacks captured-arm rechecks")
        if set(captured_exits) - {"full", "minus"} or any(
            value not in {0, 1} for value in captured_exits.values()
        ):
            raise ValueError("scorer invalidation has malformed captured-arm rechecks")
        misclassified_arms = attempt.get("misclassified_arms")
        if (
            not isinstance(misclassified_arms, list)
            or not misclassified_arms
            or any(arm not in captured_exits for arm in misclassified_arms)
            or any(captured_exits[arm] != 0 for arm in misclassified_arms)
        ):
            raise ValueError("scorer invalidation does not identify a corrected false negative")

    non_evidence = payload.get("non_evidence_attempts", [])
    if not isinstance(non_evidence, list):
        raise ValueError("real-agent non-evidence attempt register must be a list")
    for attempt in non_evidence:
        if not isinstance(attempt, dict):
            raise ValueError("real-agent non-evidence attempt must be an object")
        if attempt.get("excluded_from_counts") is not True:
            raise ValueError("incomplete real-agent attempt was not excluded from counts")
        if not isinstance(attempt.get("run_id"), str) or not isinstance(attempt.get("reason"), str):
            raise ValueError("incomplete real-agent attempt lacks an identity or reason")
    if payload.get("production_weights_changed") is not False:
        raise ValueError("real-agent evidence must not claim production activation")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _audit_count(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"label-seeking audit has an invalid {label} count")
    return value


def _validate_pinned_evidence_file(value: object, repository_root: Path) -> None:
    if not isinstance(value, dict):
        raise ValueError("real-agent evidence file binding must be an object")
    relative = value.get("path")
    expected_hash = value.get("file_sha256")
    if not isinstance(relative, str) or not _is_sha256(expected_hash):
        raise ValueError("real-agent evidence file binding is malformed")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("real-agent evidence file binding is unsafe")
    resolved = (repository_root / relative_path).resolve()
    if not resolved.is_relative_to(repository_root) or not resolved.is_file():
        raise ValueError("real-agent evidence file binding is missing")
    if _file_sha256(resolved) != expected_hash:
        raise ValueError("real-agent evidence file binding hash is stale")


def _load_pinned_evidence_json(
    value: object,
    repository_root: Path,
) -> dict[str, object]:
    _validate_pinned_evidence_file(value, repository_root)
    assert isinstance(value, dict)
    relative = value["path"]
    assert isinstance(relative, str)
    try:
        payload = json.loads((repository_root / relative).read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pinned real-agent evidence is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("pinned real-agent evidence must contain a JSON object")
    return payload


__all__ = [
    "AICalibrationReadiness",
    "AIOnlyCalibrationProtocol",
    "CalibrationEvidenceItem",
    "CalibrationGateSnapshot",
    "default_ai_calibration_protocol",
    "load_ai_calibration_protocol",
    "load_ai_calibration_readiness",
    "validate_ai_calibration_assets",
]
