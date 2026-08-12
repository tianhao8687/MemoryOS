from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from memoryos.evaluation.retrieval_weight_calibration import (
    CalibrationCandidateFeatureVector,
    CalibrationPartition,
    WeightTrainingProtocol,
)
from memoryos.retrieval_v2.scoring import (
    CALIBRATABLE_FEATURES,
    HARD_SAFETY_GATES,
    NON_NEGATIVE_FEATURES,
    NON_POSITIVE_FEATURES,
    extract_calibratable_features,
    pairwise_feature_delta,
)


def candidate_feature_vector_from_trace(
    trace: Mapping[str, Any],
    *,
    query_id: str,
    repository_id: str,
    partition: CalibrationPartition,
    candidate_id: str,
) -> CalibrationCandidateFeatureVector:
    features = extract_calibratable_features(trace)
    trace_payload = json.dumps(
        trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CalibrationCandidateFeatureVector(
        query_id=query_id,
        repository_id=repository_id,
        partition=partition,
        candidate_id=candidate_id,
        features=features,
        trace_sha256=hashlib.sha256(trace_payload).hexdigest(),
    )


def default_weight_training_protocol(
    *,
    min_train_repositories: int = 3,
) -> WeightTrainingProtocol:
    return WeightTrainingProtocol(
        feature_names=list(CALIBRATABLE_FEATURES),
        non_negative_features=list(NON_NEGATIVE_FEATURES),
        non_positive_features=list(NON_POSITIVE_FEATURES),
        hard_gate_features=list(HARD_SAFETY_GATES),
        min_train_repositories=min_train_repositories,
    )


__all__ = [
    "CALIBRATABLE_FEATURES",
    "HARD_SAFETY_GATES",
    "NON_NEGATIVE_FEATURES",
    "NON_POSITIVE_FEATURES",
    "candidate_feature_vector_from_trace",
    "default_weight_training_protocol",
    "extract_calibratable_features",
    "pairwise_feature_delta",
]
