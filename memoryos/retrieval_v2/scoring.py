from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CALIBRATABLE_FEATURES = (
    "fts_reciprocal_rank",
    "vector_reciprocal_rank",
    "graph_reciprocal_rank",
    "temporal_reciprocal_rank",
    "freshness_fresh",
    "freshness_suspect",
    "scope_task",
    "scope_branch",
    "scope_repository",
    "scope_workspace",
    "scope_user",
    "truth_resolved",
    "truth_contested",
    "evidence_log1p",
    "helpful_feedback_log1p",
    "unhelpful_feedback_log1p",
    "memory_confidence",
    "memory_importance",
    "reranker_score",
)

HARD_SAFETY_GATES = (
    "archived_exclusion",
    "cross_scope_exclusion",
    "future_information_exclusion",
    "known_time_exclusion",
    "privacy_scope_exclusion",
    "stale_exclusion",
    "valid_time_exclusion",
)

NON_NEGATIVE_FEATURES = (
    "fts_reciprocal_rank",
    "vector_reciprocal_rank",
    "graph_reciprocal_rank",
    "temporal_reciprocal_rank",
    "freshness_fresh",
    "scope_task",
    "scope_branch",
    "scope_repository",
    "scope_workspace",
    "scope_user",
    "truth_resolved",
    "evidence_log1p",
    "helpful_feedback_log1p",
    "memory_confidence",
    "memory_importance",
    "reranker_score",
)

NON_POSITIVE_FEATURES = (
    "freshness_suspect",
    "unhelpful_feedback_log1p",
)


class ShadowRetrievalProfile(BaseModel):
    """Candidate-only scoring projection; the production service never loads it implicitly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["candidate_shadow"] = "candidate_shadow"
    source_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    weights: dict[str, float]
    mmr_lambda: float = Field(ge=0.0, le=1.0)
    context_utility_mode: Literal["profile_score_per_character"] = "profile_score_per_character"
    structural_parameter_source: Literal["frozen_baseline_subject_to_sealed_promotion"] = (
        "frozen_baseline_subject_to_sealed_promotion"
    )
    production_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_weights(self) -> ShadowRetrievalProfile:
        if set(self.weights) != set(CALIBRATABLE_FEATURES):
            missing = sorted(set(CALIBRATABLE_FEATURES) - set(self.weights))
            unknown = sorted(set(self.weights) - set(CALIBRATABLE_FEATURES))
            raise ValueError(
                f"shadow profile feature mismatch; missing={missing}, unknown={unknown}"
            )
        if any(not math.isfinite(value) for value in self.weights.values()):
            raise ValueError("shadow profile weights must be finite")
        if any(self.weights[feature] < 0 for feature in NON_NEGATIVE_FEATURES):
            raise ValueError("shadow profile violates a non-negative feature constraint")
        if any(self.weights[feature] > 0 for feature in NON_POSITIVE_FEATURES):
            raise ValueError("shadow profile violates a non-positive feature constraint")
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

    def score(self, trace: Mapping[str, Any]) -> float:
        features = extract_calibratable_features(trace)
        logit = sum(self.weights[name] * features[name] for name in CALIBRATABLE_FEATURES)
        return _sigmoid(logit)


def load_shadow_retrieval_profile(path: Path) -> ShadowRetrievalProfile:
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid shadow retrieval profile: {resolved}") from exc
    return ShadowRetrievalProfile.model_validate(payload)


def extract_calibratable_features(trace: Mapping[str, Any]) -> dict[str, float]:
    freshness = str(trace.get("freshness", "unknown"))
    if freshness == "stale":
        raise ValueError("stale candidates are a hard exclusion, not a learnable feature")
    if freshness not in {"fresh", "unknown", "suspect"}:
        raise ValueError(f"unknown freshness state: {freshness}")
    scope = str(trace.get("scope_match", "other"))
    if scope not in {"task", "branch", "repository", "workspace", "user"}:
        scope = "other"
    truth = str(trace.get("truth_state", "unknown"))
    if truth not in {"resolved", "contested", "unknown"}:
        truth = "unknown"
    evidence_count = _nonnegative_int(trace.get("evidence_count", 0), "evidence_count")
    helpful = _nonnegative_int(
        trace.get("helpful_feedback_count", 0),
        "helpful_feedback_count",
    )
    unhelpful = _nonnegative_int(
        trace.get("unhelpful_feedback_count", 0),
        "unhelpful_feedback_count",
    )
    reranker = trace.get("reranker_score")
    reranker_score = 0.0 if reranker is None else float(reranker)
    if not math.isfinite(reranker_score):
        raise ValueError("reranker_score must be finite")
    return {
        "fts_reciprocal_rank": _reciprocal_rank(trace.get("fts_rank"), "fts_rank"),
        "vector_reciprocal_rank": _reciprocal_rank(trace.get("vector_rank"), "vector_rank"),
        "graph_reciprocal_rank": _reciprocal_rank(trace.get("graph_rank"), "graph_rank"),
        "temporal_reciprocal_rank": _reciprocal_rank(trace.get("temporal_rank"), "temporal_rank"),
        "freshness_fresh": float(freshness == "fresh"),
        "freshness_suspect": float(freshness == "suspect"),
        "scope_task": float(scope == "task"),
        "scope_branch": float(scope == "branch"),
        "scope_repository": float(scope == "repository"),
        "scope_workspace": float(scope == "workspace"),
        "scope_user": float(scope == "user"),
        "truth_resolved": float(truth == "resolved"),
        "truth_contested": float(truth == "contested"),
        "evidence_log1p": math.log1p(evidence_count),
        "helpful_feedback_log1p": math.log1p(helpful),
        "unhelpful_feedback_log1p": math.log1p(unhelpful),
        "memory_confidence": _unit_float(trace.get("memory_confidence"), "memory_confidence"),
        "memory_importance": _unit_float(trace.get("memory_importance"), "memory_importance"),
        "reranker_score": reranker_score,
    }


def pairwise_feature_delta(
    candidate_a_trace: Mapping[str, Any],
    candidate_b_trace: Mapping[str, Any],
) -> dict[str, float]:
    left = extract_calibratable_features(candidate_a_trace)
    right = extract_calibratable_features(candidate_b_trace)
    return {feature: left[feature] - right[feature] for feature in CALIBRATABLE_FEATURES}


def _reciprocal_rank(value: object, field_name: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive integer")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be a positive integer")
    rank = int(numeric)
    if rank < 1 or numeric != rank:
        raise ValueError(f"{field_name} must be a positive integer")
    return 1.0 / rank


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative integer")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be a non-negative integer")
    parsed = int(numeric)
    if parsed < 0 or numeric != parsed:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return parsed


def _unit_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be within [0, 1]")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{field_name} must be within [0, 1]")
    return numeric


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-value)
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


__all__ = [
    "CALIBRATABLE_FEATURES",
    "HARD_SAFETY_GATES",
    "NON_NEGATIVE_FEATURES",
    "NON_POSITIVE_FEATURES",
    "ShadowRetrievalProfile",
    "extract_calibratable_features",
    "load_shadow_retrieval_profile",
    "pairwise_feature_delta",
]
