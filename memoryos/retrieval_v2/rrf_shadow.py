from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RRF_CHANNELS = ("fts", "vector", "graph", "temporal")
FROZEN_RRF_WEIGHTS = {"fts": 1.0, "vector": 1.0, "graph": 0.82, "temporal": 0.9}
FROZEN_RRF_K = 60
FROZEN_MMR_LAMBDA = 0.78


class RRFChannelShadowProfile(BaseModel):
    """Candidate-only RRF override that freezes every non-FTS/vector parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["rrf_channel_candidate_shadow"] = "rrf_channel_candidate_shadow"
    source_public_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_feature_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_vector_channel_id: str = Field(min_length=1)
    source_vector_channel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_vector_adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conversion_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel_weights: dict[str, float]
    frozen_baseline_channel_weights: dict[str, float] = Field(
        default_factory=lambda: dict(FROZEN_RRF_WEIGHTS)
    )
    rrf_k: int = FROZEN_RRF_K
    mmr_lambda: float = FROZEN_MMR_LAMBDA
    normalization: Literal["preserve_fts_vector_total_weight"] = "preserve_fts_vector_total_weight"
    identifiable_claim: Literal["relative_fts_vector_weight_only"] = (
        "relative_fts_vector_weight_only"
    )
    production_eligible: Literal[False] = False
    production_weights_changed: Literal[False] = False
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frozen_structure(self) -> RRFChannelShadowProfile:
        if set(self.channel_weights) != set(RRF_CHANNELS):
            raise ValueError("RRF channel shadow profile must cover exactly four channels")
        if self.frozen_baseline_channel_weights != FROZEN_RRF_WEIGHTS:
            raise ValueError("RRF channel shadow profile changed its frozen baseline identity")
        if any(not math.isfinite(value) or value <= 0.0 for value in self.channel_weights.values()):
            raise ValueError("RRF channel shadow weights must be finite and positive")
        for channel in ("graph", "temporal"):
            if not math.isclose(
                self.channel_weights[channel],
                FROZEN_RRF_WEIGHTS[channel],
                abs_tol=1e-12,
            ):
                raise ValueError(f"RRF channel shadow profile changed frozen {channel} weight")
        candidate_total = self.channel_weights["fts"] + self.channel_weights["vector"]
        baseline_total = FROZEN_RRF_WEIGHTS["fts"] + FROZEN_RRF_WEIGHTS["vector"]
        if not math.isclose(candidate_total, baseline_total, abs_tol=1e-12):
            raise ValueError("RRF channel shadow profile changed FTS/vector total scale")
        if self.rrf_k != FROZEN_RRF_K:
            raise ValueError("RRF channel shadow profile changed frozen RRF K")
        if not math.isclose(self.mmr_lambda, FROZEN_MMR_LAMBDA, abs_tol=1e-12):
            raise ValueError("RRF channel shadow profile changed frozen Lexical MMR lambda")
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


def load_rrf_channel_shadow_profile(path: Path) -> RRFChannelShadowProfile:
    resolved = path.resolve(strict=True)
    try:
        payload = json.loads(resolved.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid RRF channel shadow profile: {resolved}") from exc
    return RRFChannelShadowProfile.model_validate(payload)


__all__ = [
    "FROZEN_MMR_LAMBDA",
    "FROZEN_RRF_K",
    "FROZEN_RRF_WEIGHTS",
    "RRF_CHANNELS",
    "RRFChannelShadowProfile",
    "load_rrf_channel_shadow_profile",
]
