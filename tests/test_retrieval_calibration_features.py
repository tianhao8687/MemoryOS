from __future__ import annotations

import pytest

from memoryos.evaluation.retrieval_calibration_features import (
    CALIBRATABLE_FEATURES,
    HARD_SAFETY_GATES,
    NON_POSITIVE_FEATURES,
    candidate_feature_vector_from_trace,
    default_weight_training_protocol,
    extract_calibratable_features,
    pairwise_feature_delta,
)
from memoryos.evaluation.retrieval_weight_calibration import CalibrationPartition
from memoryos.retrieval_v2.scoring import ShadowRetrievalProfile


def _trace() -> dict[str, object]:
    return {
        "fts_rank": 2,
        "vector_rank": 4,
        "graph_rank": None,
        "temporal_rank": 1,
        "freshness": "fresh",
        "scope_match": "repository",
        "truth_state": "resolved",
        "evidence_count": 3,
        "helpful_feedback_count": 2,
        "unhelpful_feedback_count": 1,
        "memory_confidence": 0.9,
        "memory_importance": 0.8,
        "reranker_score": 0.8,
    }


def test_raw_trace_features_do_not_reuse_current_magic_factors() -> None:
    features = extract_calibratable_features(_trace())

    assert tuple(features) == CALIBRATABLE_FEATURES
    assert features["fts_reciprocal_rank"] == 0.5
    assert features["vector_reciprocal_rank"] == 0.25
    assert features["temporal_reciprocal_rank"] == 1.0
    assert features["freshness_fresh"] == 1.0
    assert features["scope_repository"] == 1.0
    assert "freshness_factor" not in features
    assert "scope_factor" not in features


def test_pairwise_feature_delta_is_antisymmetric() -> None:
    left = _trace()
    right = {
        **_trace(),
        "fts_rank": 5,
        "freshness": "suspect",
        "scope_match": "workspace",
        "reranker_score": 0.2,
    }

    forward = pairwise_feature_delta(left, right)
    reverse = pairwise_feature_delta(right, left)

    assert all(forward[name] == pytest.approx(-reverse[name]) for name in CALIBRATABLE_FEATURES)

    vector = candidate_feature_vector_from_trace(
        left,
        query_id="query",
        repository_id="repository",
        partition=CalibrationPartition.TRAIN,
        candidate_id="candidate",
    )
    assert vector.features == extract_calibratable_features(left)
    assert len(vector.trace_sha256) == 64


def test_stale_is_a_hard_gate_and_never_a_learned_weight() -> None:
    with pytest.raises(ValueError, match="hard exclusion"):
        extract_calibratable_features({**_trace(), "freshness": "stale"})

    protocol = default_weight_training_protocol()
    assert set(protocol.feature_names).isdisjoint(HARD_SAFETY_GATES)
    assert protocol.hard_gate_features == list(HARD_SAFETY_GATES)
    assert protocol.non_positive_features == list(NON_POSITIVE_FEATURES)
    assert "unhelpful_feedback_log1p" in protocol.non_positive_features
    assert "freshness_unknown" not in protocol.feature_names
    assert "scope_other" not in protocol.feature_names
    assert "truth_unknown" not in protocol.feature_names


def test_shadow_profile_is_hashable_bounded_and_uses_raw_features() -> None:
    weights = {name: 0.0 for name in CALIBRATABLE_FEATURES}
    weights["memory_importance"] = 2.0
    profile = ShadowRetrievalProfile(
        source_profile_sha256="a" * 64,
        training_protocol_sha256="b" * 64,
        weights=weights,
        mmr_lambda=0.78,
    )

    score = profile.score(_trace())

    assert 0.5 < score < 1.0
    assert len(profile.digest()) == 64
    assert profile.production_eligible is False
