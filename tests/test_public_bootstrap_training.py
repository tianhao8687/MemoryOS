from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import (
    PUBLIC_BOOTSTRAP_FEATURES,
    PublicRelevanceCandidate,
    PublicRelevanceDataset,
    PublicRelevanceQuery,
    build_public_feature_rows,
    train_public_bootstrap_profile,
)
from memoryos.retrieval_v2.scoring import ShadowRetrievalProfile


def test_checked_in_public_dataset_trains_a_deterministic_nonproduction_prior() -> None:
    dataset = Path("benchmarks/calibration_v1/data")
    first = train_public_bootstrap_profile(dataset, iterations=300)
    second = train_public_bootstrap_profile(dataset, iterations=300)

    assert first == second
    assert first.status == "public_bootstrap_prior"
    assert first.production_eligible is False
    assert first.production_weights_changed is False
    assert first.vector_channel_id == "memoryos-tfidf-cosine-v1"
    assert len(first.vector_channel_source_sha256) == 64
    assert len(first.vector_feature_adapter_sha256) == 64
    assert len(first.feature_rows_sha256) == 64
    assert first.sample_weighting == (
        "equal_relevance_strata_then_equal_query_then_equal_repository"
    )
    assert first.metric_aggregation == "repository_macro_average"
    assert first.max_preference_pairs_per_query == 256
    assert first.l2_candidates == [0.001, 0.005, 0.02, 0.08, 0.32, 1.28]
    assert set(first.learned_features) == set(PUBLIC_BOOTSTRAP_FEATURES)
    assert sum(first.relative_weights.values()) == pytest.approx(1.0)
    assert first.training_repositories == [
        "encode-httpx",
        "expressjs-express",
        "pallets-markupsafe",
        "vitejs-vite",
    ]
    assert first.development_repositories == ["tokio-bytes"]
    assert first.test_repositories == ["jesseduffield-lazygit"]
    assert first.metrics["train"].queries == 200
    assert first.metrics["dev"].queries == 50
    assert first.metrics["test"].queries == 50
    assert first.metrics["train"].preference_pairs > 0
    assert first.metrics["dev"].pairwise_log_loss is not None
    assert first.metrics["test"].required_recall_at_5 is not None
    assert len(first.profile_sha256) == 64
    assert first.digest() == first.profile_sha256
    with pytest.raises(ValidationError):
        ShadowRetrievalProfile.model_validate(first.model_dump(mode="json"))


def test_feature_statistics_cannot_see_candidates_outside_the_query_pool() -> None:
    query = PublicRelevanceQuery(
        query_id="q-1",
        repository_id="example/repo",
        split=CalibrationSplit.TRAIN,
        query="repair parser",
        candidate_ids=("visible-1", "visible-2"),
    )

    def dataset(future_text: str) -> PublicRelevanceDataset:
        return PublicRelevanceDataset(
            dataset_id="future-leak-regression",
            dataset_sha256="a" * 64,
            source_adapter_sha256="b" * 64,
            candidates=(
                PublicRelevanceCandidate("visible-1", "example/repo", "repair parser"),
                PublicRelevanceCandidate("visible-2", "example/repo", "update docs"),
                PublicRelevanceCandidate("future", "example/repo", future_text),
            ),
            queries={
                CalibrationSplit.TRAIN: (query,),
                CalibrationSplit.DEV: (),
                CalibrationSplit.TEST: (),
            },
            judgments={split: () for split in CalibrationSplit},
            limitations=(),
        )

    before = build_public_feature_rows(dataset("parser parser parser"))
    after = build_public_feature_rows(dataset("completely unrelated future vocabulary"))

    assert before == after
