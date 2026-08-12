from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from memoryos.evaluation.public_bootstrap_training import train_public_bootstrap_profile
from memoryos.evaluation.public_shadow import rrf_channel_shadow_from_public
from memoryos.retrieval_v2.pipeline import retrieval_config_hash
from memoryos.retrieval_v2.rrf_shadow import (
    FROZEN_RRF_WEIGHTS,
    RRFChannelShadowProfile,
)


def test_public_prior_projects_only_fts_vector_ratio_into_frozen_rrf() -> None:
    public = train_public_bootstrap_profile(
        Path("benchmarks/calibration_v1/data"),
        iterations=300,
    )
    shadow = rrf_channel_shadow_from_public(public)

    assert shadow.production_eligible is False
    assert shadow.production_weights_changed is False
    assert shadow.source_public_profile_sha256 == public.profile_sha256
    assert shadow.source_vector_channel_id == public.vector_channel_id
    assert shadow.source_vector_channel_sha256 == public.vector_channel_source_sha256
    assert shadow.source_vector_adapter_sha256 == public.vector_feature_adapter_sha256
    assert shadow.channel_weights["graph"] == FROZEN_RRF_WEIGHTS["graph"]
    assert shadow.channel_weights["temporal"] == FROZEN_RRF_WEIGHTS["temporal"]
    assert shadow.channel_weights["fts"] + shadow.channel_weights["vector"] == pytest.approx(2.0)
    source_ratio = (
        public.relative_weights["fts_reciprocal_rank"]
        / public.relative_weights["vector_reciprocal_rank"]
    )
    assert shadow.channel_weights["fts"] / shadow.channel_weights["vector"] == pytest.approx(
        source_ratio
    )
    assert retrieval_config_hash(rrf_channel_profile=shadow) != retrieval_config_hash()


def test_rrf_shadow_rejects_changes_to_frozen_channels() -> None:
    payload = {
        "source_public_profile_sha256": "a" * 64,
        "source_dataset_sha256": "b" * 64,
        "source_feature_rows_sha256": "c" * 64,
        "source_vector_channel_id": "fastembed:BAAI/bge-small-en-v1.5@revision",
        "source_vector_channel_sha256": "d" * 64,
        "source_vector_adapter_sha256": "e" * 64,
        "conversion_source_sha256": "f" * 64,
        "channel_weights": {"fts": 0.4, "vector": 1.6, "graph": 0.81, "temporal": 0.9},
        "limitations": ["diagnostic only"],
    }
    with pytest.raises(ValidationError, match="frozen graph"):
        RRFChannelShadowProfile.model_validate(payload)
