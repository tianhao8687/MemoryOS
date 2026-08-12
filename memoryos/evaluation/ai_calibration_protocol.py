from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from memoryos.evaluation.ai_jury import AIJuryProtocol
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
        _validate_evidence_semantics(artifact, artifact_path)
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
    payload = path.read_bytes()
    if path.suffix.lower() in {".json", ".jsonl", ".md", ".patch"}:
        payload = payload.replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def _validate_evidence_semantics(
    artifact: CalibrationEvidenceItem,
    artifact_path: Path,
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
    elif artifact.artifact_id == "requests-6028-real-agent-ablation-v1":
        runtime = payload.get("runtime")
        pairs = payload.get("pairs")
        aggregate = payload.get("aggregate")
        training = payload.get("training_observation")
        if not isinstance(runtime, dict) or runtime.get("evidence_type") != "real_coding_agent":
            raise ValueError("real-agent ablation evidence has invalid runtime metadata")
        if not isinstance(pairs, list) or len(pairs) != artifact.executable_ablation_pairs:
            raise ValueError("real-agent ablation pair count is stale")
        if artifact.records != 2 * len(pairs) or artifact.real_agent_tasks != 1:
            raise ValueError("real-agent ablation record or task count is stale")
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("real-agent ablation pair must be an object")
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
        if not isinstance(aggregate, dict) or aggregate.get("safety_worsened_pairs") != 0:
            raise ValueError("real-agent ablation aggregate failed its safety gate")
        if aggregate.get("production_eligible") is not False:
            raise ValueError("real-agent ablation evidence cannot be promotion evidence")
        if not isinstance(training, dict) or training.get("real_executable_labels") != 1:
            raise ValueError("real-agent training-observation count is stale")
        if payload.get("production_weights_changed") is not False:
            raise ValueError("real-agent evidence must not claim production activation")
    else:
        raise ValueError(f"unknown checked-in evidence artifact: {artifact.artifact_id}")


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
