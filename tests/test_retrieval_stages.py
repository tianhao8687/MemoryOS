from __future__ import annotations

import pytest

from memoryos.retrieval_v2.fusion import reciprocal_rank_fusion
from memoryos.retrieval_v2.stages import (
    NORMALIZED_SCORE_CONTRACT,
    FusionStage,
    normalize_weighted_rrf,
)


def test_normalized_weighted_rrf_has_recipe_independent_theoretical_ceiling() -> None:
    one_channel = {"fts": ["a", "b"]}
    two_channels = {"fts": ["a", "b"], "vector": ["a", "b"]}
    weights = {"fts": 1.0, "vector": 1.0}
    one_raw, _ = reciprocal_rank_fusion(one_channel, weights=weights, k=60)
    two_raw, _ = reciprocal_rank_fusion(two_channels, weights=weights, k=60)

    one = normalize_weighted_rrf(one_raw, rankings=one_channel, weights=weights, k=60)
    two = normalize_weighted_rrf(two_raw, rankings=two_channels, weights=weights, k=60)

    assert one["a"] == pytest.approx(1.0)
    assert two["a"] == pytest.approx(1.0)
    assert 0.0 < one["b"] < one["a"]
    assert 0.0 < two["b"] < two["a"]


def test_fusion_stage_names_and_bounds_the_candidate_score_contract() -> None:
    result = FusionStage().execute(
        {"fts": ["a", "b"], "source_anchor": ["b"]},
        weights={"fts": 1.0, "source_anchor": 1.0},
        k=60,
        normalized=True,
    )

    assert result.score_contract == NORMALIZED_SCORE_CONTRACT
    assert all(0.0 <= score <= 1.0 for score in result.scores.values())
    assert result.rank_traces["b"] == {"fts": 2, "source_anchor": 1}
