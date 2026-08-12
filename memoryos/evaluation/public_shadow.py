from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memoryos.evaluation.public_bootstrap_training import PublicBootstrapProfile
from memoryos.retrieval_v2.rrf_shadow import (
    FROZEN_RRF_WEIGHTS,
    RRFChannelShadowProfile,
)


def rrf_channel_shadow_from_public(
    profile: PublicBootstrapProfile,
) -> RRFChannelShadowProfile:
    """Project only the identified FTS/vector ratio into frozen RRF structure."""

    if profile.production_eligible or profile.production_weights_changed:
        raise ValueError("public bootstrap profile must remain non-production")
    fts = profile.relative_weights["fts_reciprocal_rank"]
    vector = profile.relative_weights["vector_reciprocal_rank"]
    total = fts + vector
    if total <= 0.0:
        raise ValueError("public bootstrap profile has no FTS/vector mass")
    baseline_total = FROZEN_RRF_WEIGHTS["fts"] + FROZEN_RRF_WEIGHTS["vector"]
    return RRFChannelShadowProfile(
        source_public_profile_sha256=profile.profile_sha256,
        source_dataset_sha256=profile.source_dataset_sha256,
        source_feature_rows_sha256=profile.feature_rows_sha256,
        source_vector_channel_id=profile.vector_channel_id,
        source_vector_channel_sha256=profile.vector_channel_source_sha256,
        source_vector_adapter_sha256=profile.vector_feature_adapter_sha256,
        conversion_source_sha256=public_shadow_converter_digest(),
        channel_weights={
            "fts": baseline_total * fts / total,
            "vector": baseline_total * vector / total,
            "graph": FROZEN_RRF_WEIGHTS["graph"],
            "temporal": FROZEN_RRF_WEIGHTS["temporal"],
        },
        limitations=[
            *profile.limitations,
            "This projection changes only the FTS/vector RRF ratio; graph, temporal, RRF K, "
            "MMR, freshness, scope, feedback, truth, and safety gates stay frozen.",
            "The projection is diagnostic-only and cannot authorize production activation.",
        ],
    )


def load_public_bootstrap_profile(path: Path) -> PublicBootstrapProfile:
    resolved = path.resolve(strict=True)
    try:
        payload = json.loads(resolved.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid public bootstrap profile: {resolved}") from exc
    return PublicBootstrapProfile.model_validate(payload)


def public_shadow_converter_digest() -> str:
    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "load_public_bootstrap_profile",
    "public_shadow_converter_digest",
    "rrf_channel_shadow_from_public",
]
