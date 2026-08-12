from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from memoryos.evaluation.calibration_models import (
    CalibrationDatasetBundle,
    CalibrationSplit,
    load_calibration_dataset,
)

PUBLIC_BOOTSTRAP_FEATURES = (
    "fts_reciprocal_rank",
    "vector_reciprocal_rank",
)
DEFAULT_PUBLIC_VECTOR_CHANNEL_ID = "memoryos-tfidf-cosine-v1"
MAX_PREFERENCE_PAIRS_PER_QUERY = 256

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicBootstrapMetrics(StrictModel):
    queries: int = Field(ge=0)
    eligible_candidates: int = Field(ge=0)
    preference_pairs: int = Field(ge=0)
    pairwise_log_loss: float | None = Field(default=None, ge=0.0)
    pairwise_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    ndcg_at_10: float | None = Field(default=None, ge=0.0, le=1.0)
    required_recall_at_5: float | None = Field(default=None, ge=0.0, le=1.0)
    reciprocal_rank: float | None = Field(default=None, ge=0.0, le=1.0)


class PublicBootstrapProfile(StrictModel):
    """Public relevance prior that cannot be loaded by the production retrieval path."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["public_bootstrap_prior"] = "public_bootstrap_prior"
    source_dataset_id: str = Field(min_length=1)
    source_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trainer_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_channel_id: str = Field(min_length=1)
    vector_channel_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_feature_adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    learned_features: list[str]
    unidentified_features_frozen: list[str]
    raw_weights: dict[str, float]
    relative_weights: dict[str, float]
    selected_l2: float = Field(gt=0.0)
    l2_candidates: list[float] = Field(min_length=1)
    iterations: int = Field(ge=100)
    learning_rate: float = Field(gt=0.0)
    max_preference_pairs_per_query: int = Field(gt=0)
    sample_weighting: Literal["equal_relevance_strata_then_equal_query_then_equal_repository"] = (
        "equal_relevance_strata_then_equal_query_then_equal_repository"
    )
    metric_aggregation: Literal["repository_macro_average"] = "repository_macro_average"
    training_repositories: list[str]
    development_repositories: list[str]
    test_repositories: list[str]
    metrics: dict[str, PublicBootstrapMetrics]
    equal_weight_baseline_metrics: dict[str, PublicBootstrapMetrics]
    candidate_beats_equal_weight_baseline_on_dev: bool
    leave_one_repository_out_relative_ranges: dict[str, list[float]]
    identifiable_claim: Literal["relative_fts_vector_weight_within_public_code_relevance_only"] = (
        "relative_fts_vector_weight_within_public_code_relevance_only"
    )
    production_eligible: Literal[False] = False
    production_weights_changed: Literal[False] = False
    limitations: list[str] = Field(min_length=1)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_feature_identity(self) -> PublicBootstrapProfile:
        expected = set(self.learned_features)
        if expected != set(PUBLIC_BOOTSTRAP_FEATURES):
            raise ValueError("public bootstrap profile has the wrong learned feature set")
        if set(self.raw_weights) != expected or set(self.relative_weights) != expected:
            raise ValueError("public bootstrap weights must cover every learned feature")
        if any(not math.isfinite(value) or value < 0.0 for value in self.raw_weights.values()):
            raise ValueError("public bootstrap raw weights must be finite and non-negative")
        if any(
            not math.isfinite(value) or not 0.0 <= value <= 1.0
            for value in self.relative_weights.values()
        ):
            raise ValueError("public bootstrap relative weights must be within [0, 1]")
        if not math.isclose(sum(self.relative_weights.values()), 1.0, abs_tol=1e-9):
            raise ValueError("public bootstrap relative weights must sum to one")
        if self.digest() != self.profile_sha256:
            raise ValueError("public bootstrap profile content does not match profile_sha256")
        return self

    def digest(self) -> str:
        return _canonical_hash(self.model_dump(mode="json", exclude={"profile_sha256"}))


@dataclass(frozen=True)
class PublicRelevanceCandidate:
    id: str
    repository_id: str
    text: str


@dataclass(frozen=True)
class PublicRelevanceQuery:
    query_id: str
    repository_id: str
    split: CalibrationSplit
    query: str
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class PublicRelevanceJudgment:
    query_id: str
    candidate_id: str
    relevance: int
    eligible: bool
    required: bool


@dataclass(frozen=True)
class PublicRelevanceDataset:
    dataset_id: str
    dataset_sha256: str
    source_adapter_sha256: str
    candidates: tuple[PublicRelevanceCandidate, ...]
    queries: Mapping[CalibrationSplit, tuple[PublicRelevanceQuery, ...]]
    judgments: Mapping[CalibrationSplit, tuple[PublicRelevanceJudgment, ...]]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class PublicFeatureRow:
    query_id: str
    repository_id: str
    split: CalibrationSplit
    candidate_id: str
    values: tuple[float, float]


@dataclass(frozen=True)
class _PreferencePair:
    repository_id: str
    split: CalibrationSplit
    feature_delta: tuple[float, float]
    sample_weight: float
    pair_count: int


def train_public_bootstrap_profile(
    dataset_root: Path,
    *,
    l2_candidates: Sequence[float] = (0.001, 0.005, 0.02, 0.08, 0.32, 1.28),
    iterations: int = 2500,
    learning_rate: float = 0.25,
    max_preference_pairs_per_query: int = MAX_PREFERENCE_PAIRS_PER_QUERY,
) -> PublicBootstrapProfile:
    """Fit a prior from the checked-in, pinned public Git calibration dataset."""

    bundle = load_calibration_dataset(dataset_root)
    dataset = public_relevance_dataset_from_calibration(bundle)
    return train_public_relevance_profile(
        dataset,
        l2_candidates=l2_candidates,
        iterations=iterations,
        learning_rate=learning_rate,
        max_preference_pairs_per_query=max_preference_pairs_per_query,
    )


def train_public_relevance_profile(
    dataset: PublicRelevanceDataset,
    *,
    feature_rows: Sequence[PublicFeatureRow] | None = None,
    vector_channel_id: str = DEFAULT_PUBLIC_VECTOR_CHANNEL_ID,
    vector_channel_source_sha256: str | None = None,
    vector_feature_adapter_sha256: str | None = None,
    vector_channel_limitations: Sequence[str] = (),
    l2_candidates: Sequence[float] = (0.001, 0.005, 0.02, 0.08, 0.32, 1.28),
    iterations: int = 2500,
    learning_rate: float = 0.25,
    max_preference_pairs_per_query: int = MAX_PREFERENCE_PAIRS_PER_QUERY,
) -> PublicBootstrapProfile:
    """Fit a reproducible, non-production FTS/vector relative-weight prior."""

    if not l2_candidates or any(
        not math.isfinite(value) or value <= 0.0 for value in l2_candidates
    ):
        raise ValueError("l2_candidates must contain positive finite values")
    if iterations < 100:
        raise ValueError("public bootstrap training requires at least 100 iterations")
    if not math.isfinite(learning_rate) or learning_rate <= 0.0:
        raise ValueError("learning_rate must be positive and finite")
    if max_preference_pairs_per_query < 1:
        raise ValueError("max_preference_pairs_per_query must be positive")
    if not vector_channel_id.strip():
        raise ValueError("vector_channel_id cannot be empty")

    trainer_sha256 = public_bootstrap_trainer_digest()
    resolved_vector_sha256 = vector_channel_source_sha256 or trainer_sha256
    if not _is_sha256(resolved_vector_sha256):
        raise ValueError("vector_channel_source_sha256 must be a lowercase SHA-256")
    resolved_vector_adapter_sha256 = vector_feature_adapter_sha256 or trainer_sha256
    if not _is_sha256(resolved_vector_adapter_sha256):
        raise ValueError("vector_feature_adapter_sha256 must be a lowercase SHA-256")
    resolved_feature_rows = (
        build_public_feature_rows(dataset) if feature_rows is None else list(feature_rows)
    )
    _validate_feature_rows(dataset, resolved_feature_rows)
    pairs = _preference_pairs(
        dataset,
        resolved_feature_rows,
        max_pairs_per_query=max_preference_pairs_per_query,
    )
    training = [item for item in pairs if item.split is CalibrationSplit.TRAIN]
    development = [item for item in pairs if item.split is CalibrationSplit.DEV]
    if not training or not development:
        raise ValueError("public bootstrap training requires train and development pairs")

    scales = _feature_scales(training)
    candidates = [
        (
            float(l2),
            _fit_weights(
                training,
                scales=scales,
                l2=float(l2),
                iterations=iterations,
                learning_rate=learning_rate,
            ),
        )
        for l2 in sorted(set(l2_candidates))
    ]
    selected_l2, learned = min(
        candidates,
        key=lambda item: (_pairwise_log_loss(development, item[1]), item[0]),
    )
    baseline = np.ones(len(PUBLIC_BOOTSTRAP_FEATURES), dtype=np.float64)
    metrics = _all_metrics(dataset, resolved_feature_rows, pairs, learned)
    baseline_metrics = _all_metrics(dataset, resolved_feature_rows, pairs, baseline)
    candidate_dev = metrics[CalibrationSplit.DEV.value].pairwise_log_loss
    baseline_dev = baseline_metrics[CalibrationSplit.DEV.value].pairwise_log_loss
    assert candidate_dev is not None and baseline_dev is not None

    relative = _relative_weights(learned)
    leave_one_out = _leave_one_repository_out_ranges(
        training,
        scales=scales,
        l2=selected_l2,
        iterations=iterations,
        learning_rate=learning_rate,
    )
    repositories = {
        split: sorted({query.repository_id for query in dataset.queries[split]})
        for split in CalibrationSplit
    }
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "status": "public_bootstrap_prior",
        "source_dataset_id": dataset.dataset_id,
        "source_dataset_sha256": dataset.dataset_sha256,
        "source_adapter_sha256": dataset.source_adapter_sha256,
        "trainer_source_sha256": trainer_sha256,
        "vector_channel_id": vector_channel_id,
        "vector_channel_source_sha256": resolved_vector_sha256,
        "vector_feature_adapter_sha256": resolved_vector_adapter_sha256,
        "feature_rows_sha256": public_feature_rows_digest(resolved_feature_rows),
        "learned_features": list(PUBLIC_BOOTSTRAP_FEATURES),
        "unidentified_features_frozen": [
            "freshness_and_staleness",
            "graph_rank",
            "scope_levels",
            "truth_state",
            "evidence_and_feedback",
            "memory_confidence_and_importance",
            "reranker_score",
            "absolute_scale_against_other_feature_groups",
        ],
        "raw_weights": {
            name: round(float(learned[index]), 12)
            for index, name in enumerate(PUBLIC_BOOTSTRAP_FEATURES)
        },
        "relative_weights": {
            name: round(float(relative[index]), 12)
            for index, name in enumerate(PUBLIC_BOOTSTRAP_FEATURES)
        },
        "selected_l2": selected_l2,
        "l2_candidates": sorted(set(float(value) for value in l2_candidates)),
        "iterations": iterations,
        "learning_rate": learning_rate,
        "max_preference_pairs_per_query": max_preference_pairs_per_query,
        "sample_weighting": ("equal_relevance_strata_then_equal_query_then_equal_repository"),
        "metric_aggregation": "repository_macro_average",
        "training_repositories": repositories[CalibrationSplit.TRAIN],
        "development_repositories": repositories[CalibrationSplit.DEV],
        "test_repositories": repositories[CalibrationSplit.TEST],
        "metrics": {split: value.model_dump(mode="json") for split, value in metrics.items()},
        "equal_weight_baseline_metrics": {
            split: value.model_dump(mode="json") for split, value in baseline_metrics.items()
        },
        "candidate_beats_equal_weight_baseline_on_dev": candidate_dev < baseline_dev,
        "leave_one_repository_out_relative_ranges": leave_one_out,
        "identifiable_claim": ("relative_fts_vector_weight_within_public_code_relevance_only"),
        "production_eligible": False,
        "production_weights_changed": False,
        "limitations": [
            *dataset.limitations,
            *(
                vector_channel_limitations
                if vector_channel_limitations
                else (
                    "The public vector channel is a TF-IDF cosine proxy, not the production "
                    "semantic embedding provider.",
                )
            ),
            "Only the relative FTS/vector blend is identified; every other production "
            "feature remains frozen.",
            "The held-out test repository is evaluated only after development selection "
            "and is public, not sealed.",
            "This artifact cannot be loaded by the production or shadow retrieval profile schema.",
            "Executable full/minus-memory evidence is still required for causal calibration "
            "and promotion.",
        ],
    }
    profile_hash = _canonical_hash(payload)
    return PublicBootstrapProfile.model_validate({**payload, "profile_sha256": profile_hash})


def public_relevance_dataset_from_calibration(
    bundle: CalibrationDatasetBundle,
) -> PublicRelevanceDataset:
    return PublicRelevanceDataset(
        dataset_id=bundle.manifest.dataset_id,
        dataset_sha256=bundle.digest,
        source_adapter_sha256=public_bootstrap_trainer_digest(),
        candidates=tuple(
            PublicRelevanceCandidate(
                id=candidate.id,
                repository_id=candidate.repository_id,
                text=candidate.text,
            )
            for candidate in bundle.candidates
        ),
        queries={
            split: tuple(
                PublicRelevanceQuery(
                    query_id=query.id,
                    repository_id=query.repository_id,
                    split=split,
                    query=query.query,
                    candidate_ids=tuple(query.candidate_ids),
                )
                for query in bundle.queries[split]
            )
            for split in CalibrationSplit
        },
        judgments={
            split: tuple(
                PublicRelevanceJudgment(
                    query_id=judgment.query_id,
                    candidate_id=judgment.candidate_id,
                    relevance=judgment.relevance,
                    eligible=judgment.eligibility.value == "eligible",
                    required=judgment.required,
                )
                for judgment in bundle.judgments[split]
            )
            for split in CalibrationSplit
        },
        limitations=tuple(bundle.manifest.limitations),
    )


def build_public_feature_rows(
    dataset: PublicRelevanceDataset,
    *,
    vector_scores_by_query: Mapping[str, Mapping[str, float]] | None = None,
) -> list[PublicFeatureRow]:
    """Build runtime-only ranks before any scorer qrels are consulted."""

    candidates = {candidate.id: candidate for candidate in dataset.candidates}
    token_counts = {
        candidate.id: Counter(_tokens(candidate.text)) for candidate in dataset.candidates
    }
    rows: list[PublicFeatureRow] = []
    used_vector_query_ids: set[str] = set()
    for split in CalibrationSplit:
        for query in dataset.queries[split]:
            document_frequencies: Counter[str] = Counter()
            for candidate_id in query.candidate_ids:
                document_frequencies.update(token_counts[candidate_id].keys())
            total_documents = len(query.candidate_ids)
            average_length = sum(
                sum(token_counts[candidate_id].values()) for candidate_id in query.candidate_ids
            ) / max(total_documents, 1)
            bm25_idf = {
                token: math.log(1.0 + (total_documents - frequency + 0.5) / (frequency + 0.5))
                for token, frequency in document_frequencies.items()
            }
            query_counts = Counter(_tokens(query.query))
            fts_scores = {
                candidate_id: _bm25_score(
                    query_counts,
                    token_counts[candidate_id],
                    bm25_idf,
                    average_length=average_length,
                )
                for candidate_id in query.candidate_ids
            }
            if vector_scores_by_query is None:
                tfidf_idf = {
                    token: math.log((total_documents + 1.0) / (frequency + 1.0)) + 1.0
                    for token, frequency in document_frequencies.items()
                }
                query_vector = _tfidf_vector(query_counts, tfidf_idf)
                vector_scores = {
                    candidate_id: _cosine(
                        query_vector,
                        _tfidf_vector(token_counts[candidate_id], tfidf_idf),
                    )
                    for candidate_id in query.candidate_ids
                }
            else:
                supplied = vector_scores_by_query.get(query.query_id)
                if supplied is None:
                    raise ValueError(f"missing vector scores for query {query.query_id}")
                if set(supplied) != set(query.candidate_ids):
                    raise ValueError(
                        f"vector scores do not match candidate pool for query {query.query_id}"
                    )
                if any(not math.isfinite(float(value)) for value in supplied.values()):
                    raise ValueError(f"non-finite vector score for query {query.query_id}")
                vector_scores = {key: float(value) for key, value in supplied.items()}
                used_vector_query_ids.add(query.query_id)
            fts_ranks = _positive_reciprocal_ranks(fts_scores)
            vector_ranks = _positive_reciprocal_ranks(vector_scores)
            for candidate_id in query.candidate_ids:
                candidate = candidates[candidate_id]
                rows.append(
                    PublicFeatureRow(
                        query_id=query.query_id,
                        repository_id=query.repository_id,
                        split=split,
                        candidate_id=candidate.id,
                        values=(
                            fts_ranks.get(candidate_id, 0.0),
                            vector_ranks.get(candidate_id, 0.0),
                        ),
                    )
                )
    if vector_scores_by_query is not None and used_vector_query_ids != set(vector_scores_by_query):
        raise ValueError("vector scores contain unknown query IDs")
    return rows


def public_bootstrap_trainer_digest() -> str:
    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def public_feature_rows_digest(feature_rows: Sequence[PublicFeatureRow]) -> str:
    digest = hashlib.sha256()
    for row in sorted(
        feature_rows,
        key=lambda item: (item.split.value, item.repository_id, item.query_id, item.candidate_id),
    ):
        encoded = json.dumps(
            {
                "candidate_id": row.candidate_id,
                "query_id": row.query_id,
                "repository_id": row.repository_id,
                "split": row.split.value,
                "values": row.values,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _validate_feature_rows(
    dataset: PublicRelevanceDataset,
    feature_rows: Sequence[PublicFeatureRow],
) -> None:
    candidates = {candidate.id: candidate for candidate in dataset.candidates}
    if len(candidates) != len(dataset.candidates):
        raise ValueError("public relevance candidate IDs must be unique")
    expected: dict[tuple[str, str], tuple[str, CalibrationSplit]] = {}
    seen_query_ids: set[str] = set()
    for split in CalibrationSplit:
        for query in dataset.queries[split]:
            if query.query_id in seen_query_ids:
                raise ValueError("public relevance query IDs must be globally unique")
            seen_query_ids.add(query.query_id)
            if query.split is not split:
                raise ValueError(f"query {query.query_id} has an inconsistent split")
            for candidate_id in query.candidate_ids:
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    raise ValueError(f"query {query.query_id} references an unknown candidate")
                expected[(query.query_id, candidate_id)] = (query.repository_id, split)

    observed: dict[tuple[str, str], PublicFeatureRow] = {}
    for row in feature_rows:
        key = (row.query_id, row.candidate_id)
        if key in observed:
            raise ValueError("public feature rows contain duplicate query/candidate pairs")
        if len(row.values) != len(PUBLIC_BOOTSTRAP_FEATURES) or any(
            not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in row.values
        ):
            raise ValueError("public feature values must be finite and within [0, 1]")
        identity = expected.get(key)
        if identity is None or identity != (row.repository_id, row.split):
            raise ValueError("public feature row identity does not match its dataset query")
        observed[key] = row
    if set(observed) != set(expected):
        raise ValueError("public feature rows do not exactly cover every query candidate")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _preference_pairs(
    dataset: PublicRelevanceDataset,
    feature_rows: Sequence[PublicFeatureRow],
    *,
    max_pairs_per_query: int,
) -> list[_PreferencePair]:
    feature_index = {(row.query_id, row.candidate_id): row for row in feature_rows}
    aggregates: dict[tuple[str, CalibrationSplit, tuple[float, float]], tuple[float, int]] = {}
    for split in CalibrationSplit:
        judgments_by_query: dict[str, list[PublicRelevanceJudgment]] = {}
        for judgment in dataset.judgments[split]:
            if judgment.eligible:
                judgments_by_query.setdefault(judgment.query_id, []).append(judgment)
        for query in dataset.queries[split]:
            judgments = judgments_by_query[query.query_id]
            drafted: list[tuple[tuple[float, float], float]] = []
            for better, worse in _sample_preference_judgments(
                query.query_id,
                judgments,
                limit=max_pairs_per_query,
            ):
                better_features = feature_index[(query.query_id, better.candidate_id)].values
                worse_features = feature_index[(query.query_id, worse.candidate_id)].values
                drafted.append(
                    (
                        (
                            better_features[0] - worse_features[0],
                            better_features[1] - worse_features[1],
                        ),
                        float(better.relevance - worse.relevance),
                    )
                )
            total_gap = sum(gap for _, gap in drafted)
            if total_gap <= 0.0:
                continue
            for delta, gap in drafted:
                key = (query.repository_id, split, delta)
                previous_weight, previous_count = aggregates.get(key, (0.0, 0))
                aggregates[key] = (
                    previous_weight + gap / total_gap,
                    previous_count + 1,
                )
    repository_totals: dict[tuple[str, CalibrationSplit], float] = {}
    for (repository_id, split, _), (sample_weight, _) in aggregates.items():
        repository_key = (repository_id, split)
        repository_totals[repository_key] = (
            repository_totals.get(repository_key, 0.0) + sample_weight
        )
    return [
        _PreferencePair(
            repository_id=repository_id,
            split=split,
            feature_delta=delta,
            sample_weight=(sample_weight / repository_totals[(repository_id, split)]),
            pair_count=pair_count,
        )
        for (repository_id, split, delta), (sample_weight, pair_count) in sorted(
            aggregates.items(),
            key=lambda item: (item[0][1].value, item[0][0], item[0][2]),
        )
    ]


def _sample_preference_judgments(
    query_id: str,
    judgments: Sequence[PublicRelevanceJudgment],
    *,
    limit: int,
) -> list[tuple[PublicRelevanceJudgment, PublicRelevanceJudgment]]:
    """Deterministically cap pairs while balancing relevance-level combinations."""

    if limit <= 0:
        return []
    by_relevance: dict[int, list[PublicRelevanceJudgment]] = {}
    for judgment in judgments:
        by_relevance.setdefault(judgment.relevance, []).append(judgment)
    for relevance, items in by_relevance.items():
        items.sort(
            key=lambda item: _stable_sample_key(
                query_id,
                str(relevance),
                item.candidate_id,
            )
        )

    strata: list[
        tuple[
            int,
            int,
            list[PublicRelevanceJudgment],
            list[PublicRelevanceJudgment],
            int,
        ]
    ] = []
    levels = sorted(by_relevance, reverse=True)
    for better_level in levels:
        for worse_level in levels:
            if better_level <= worse_level:
                continue
            better_items = by_relevance[better_level]
            worse_items = by_relevance[worse_level]
            strata.append(
                (
                    better_level,
                    worse_level,
                    better_items,
                    worse_items,
                    len(better_items) * len(worse_items),
                )
            )
    if not strata:
        return []

    target = min(limit, sum(stratum[4] for stratum in strata))
    allocations = [0] * len(strata)
    for _ in range(target):
        available = [
            index for index, stratum in enumerate(strata) if allocations[index] < stratum[4]
        ]
        if not available:
            break
        selected = min(
            available,
            key=lambda index: (
                allocations[index],
                _stable_sample_key(
                    query_id,
                    str(strata[index][0]),
                    str(strata[index][1]),
                ),
            ),
        )
        allocations[selected] += 1

    sampled: list[tuple[PublicRelevanceJudgment, PublicRelevanceJudgment]] = []
    for allocation, stratum in zip(allocations, strata, strict=True):
        better_level, worse_level, better_items, worse_items, capacity = stratum
        if allocation == 0:
            continue
        start = (
            _stable_sample_integer(
                query_id,
                str(better_level),
                str(worse_level),
                "start",
            )
            % capacity
        )
        step = _coprime_step(
            capacity,
            _stable_sample_integer(
                query_id,
                str(better_level),
                str(worse_level),
                "step",
            ),
        )
        for offset in range(allocation):
            linear_index = (start + offset * step) % capacity
            better_index, worse_index = divmod(linear_index, len(worse_items))
            sampled.append((better_items[better_index], worse_items[worse_index]))
    return sampled


def _stable_sample_key(*parts: str) -> tuple[int, str]:
    return (_stable_sample_integer(*parts), parts[-1])


def _stable_sample_integer(*parts: str) -> int:
    payload = "\x1f".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _coprime_step(capacity: int, seed: int) -> int:
    if capacity <= 1:
        return 1
    step = seed % capacity or 1
    while math.gcd(step, capacity) != 1:
        step = (step + 1) % capacity or 1
    return step


def _feature_scales(pairs: Sequence[_PreferencePair]) -> np.ndarray:
    matrix, sample_weights = _pair_matrix(pairs)
    squared = np.square(matrix)
    scales = np.sqrt(
        np.sum(squared * sample_weights[:, None], axis=0) / max(float(sample_weights.sum()), 1.0)
    )
    return np.where(scales < 1e-9, 1.0, scales)


def _fit_weights(
    pairs: Sequence[_PreferencePair],
    *,
    scales: np.ndarray,
    l2: float,
    iterations: int,
    learning_rate: float,
) -> np.ndarray:
    matrix, sample_weights = _pair_matrix(pairs)
    scaled = matrix / scales
    learned = np.zeros(len(PUBLIC_BOOTSTRAP_FEATURES), dtype=np.float64)
    denominator = max(float(sample_weights.sum()), 1.0)
    for iteration in range(iterations):
        predictions = _sigmoid(scaled @ learned)
        gradient = scaled.T @ ((predictions - 1.0) * sample_weights) / denominator + l2 * learned
        step = learning_rate / math.sqrt(1.0 + iteration / 250.0)
        learned -= step * gradient
        learned = np.maximum(learned, 0.0)
    return cast(np.ndarray[Any, np.dtype[np.float64]], learned / scales)


def _pair_matrix(pairs: Sequence[_PreferencePair]) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray([pair.feature_delta for pair in pairs], dtype=np.float64)
    sample_weights = np.asarray([pair.sample_weight for pair in pairs], dtype=np.float64)
    return matrix, sample_weights


def _pairwise_log_loss(pairs: Sequence[_PreferencePair], weights: np.ndarray) -> float:
    matrix, sample_weights = _pair_matrix(pairs)
    predictions = np.clip(_sigmoid(matrix @ weights), 1e-12, 1.0 - 1e-12)
    return -float(np.sum(sample_weights * np.log(predictions)) / sample_weights.sum())


def _pairwise_accuracy(pairs: Sequence[_PreferencePair], weights: np.ndarray) -> float:
    matrix, sample_weights = _pair_matrix(pairs)
    correct = (matrix @ weights) > 0.0
    return float(np.sum(sample_weights * correct) / sample_weights.sum())


def _all_metrics(
    dataset: PublicRelevanceDataset,
    feature_rows: Sequence[PublicFeatureRow],
    pairs: Sequence[_PreferencePair],
    weights: np.ndarray,
) -> dict[str, PublicBootstrapMetrics]:
    return {
        split.value: _metrics_for_split(dataset, feature_rows, pairs, weights, split)
        for split in CalibrationSplit
    }


def _metrics_for_split(
    dataset: PublicRelevanceDataset,
    feature_rows: Sequence[PublicFeatureRow],
    pairs: Sequence[_PreferencePair],
    weights: np.ndarray,
    split: CalibrationSplit,
) -> PublicBootstrapMetrics:
    split_pairs = [pair for pair in pairs if pair.split is split]
    features = {(row.query_id, row.candidate_id): row for row in feature_rows if row.split is split}
    judgments: dict[str, dict[str, PublicRelevanceJudgment]] = {}
    for judgment in dataset.judgments[split]:
        if judgment.eligible:
            judgments.setdefault(judgment.query_id, {})[judgment.candidate_id] = judgment
    ndcgs: dict[str, list[float]] = {}
    recalls: dict[str, list[float]] = {}
    reciprocal_ranks: dict[str, list[float]] = {}
    eligible_candidates = 0
    for query in dataset.queries[split]:
        query_judgments = judgments[query.query_id]
        eligible_candidates += len(query_judgments)
        ranked = sorted(
            query_judgments,
            key=lambda candidate_id: (
                -float(np.dot(features[(query.query_id, candidate_id)].values, weights)),
                candidate_id,
            ),
        )
        relevances = [query_judgments[candidate_id].relevance for candidate_id in ranked]
        ndcgs.setdefault(query.repository_id, []).append(_ndcg_at_10(relevances))
        required = {
            candidate_id for candidate_id, judgment in query_judgments.items() if judgment.required
        }
        recalls.setdefault(query.repository_id, []).append(float(bool(required & set(ranked[:5]))))
        first_relevant = next(
            (index for index, relevance in enumerate(relevances, start=1) if relevance > 0),
            None,
        )
        reciprocal_ranks.setdefault(query.repository_id, []).append(
            0.0 if first_relevant is None else 1.0 / first_relevant
        )
    return PublicBootstrapMetrics(
        queries=len(dataset.queries[split]),
        eligible_candidates=eligible_candidates,
        preference_pairs=sum(pair.pair_count for pair in split_pairs),
        pairwise_log_loss=(None if not split_pairs else _pairwise_log_loss(split_pairs, weights)),
        pairwise_accuracy=(None if not split_pairs else _pairwise_accuracy(split_pairs, weights)),
        ndcg_at_10=_repository_macro_mean(ndcgs),
        required_recall_at_5=_repository_macro_mean(recalls),
        reciprocal_rank=_repository_macro_mean(reciprocal_ranks),
    )


def _leave_one_repository_out_ranges(
    training: Sequence[_PreferencePair],
    *,
    scales: np.ndarray,
    l2: float,
    iterations: int,
    learning_rate: float,
) -> dict[str, list[float]]:
    repositories = sorted({pair.repository_id for pair in training})
    relative_weights = []
    for repository in repositories:
        subset = [pair for pair in training if pair.repository_id != repository]
        if not subset:
            continue
        learned = _fit_weights(
            subset,
            scales=scales,
            l2=l2,
            iterations=iterations,
            learning_rate=learning_rate,
        )
        relative_weights.append(_relative_weights(learned))
    if not relative_weights:
        return {name: [0.0, 1.0] for name in PUBLIC_BOOTSTRAP_FEATURES}
    return {
        name: [
            round(float(min(values[index] for values in relative_weights)), 12),
            round(float(max(values[index] for values in relative_weights)), 12),
        ]
        for index, name in enumerate(PUBLIC_BOOTSTRAP_FEATURES)
    }


def _relative_weights(weights: np.ndarray) -> np.ndarray:
    total = float(weights.sum())
    if total <= 1e-12:
        return np.full(len(weights), 1.0 / len(weights), dtype=np.float64)
    return weights / total


def _tokens(value: str) -> tuple[str, ...]:
    expanded = _CAMEL_BOUNDARY.sub(" ", value)
    return tuple(token.lower() for token in _TOKEN_PATTERN.findall(expanded) if len(token) > 1)


def _bm25_score(
    query_counts: Counter[str],
    document_counts: Counter[str],
    idf: Mapping[str, float],
    *,
    average_length: float,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    document_length = sum(document_counts.values())
    length_factor = 1.0 - b + b * document_length / max(average_length, 1.0)
    score = 0.0
    for token in query_counts:
        frequency = document_counts.get(token, 0)
        if frequency <= 0:
            continue
        score += idf.get(token, 0.0) * frequency * (k1 + 1.0) / (frequency + k1 * length_factor)
    return score


def _tfidf_vector(
    counts: Counter[str],
    idf: Mapping[str, float],
) -> dict[str, float]:
    return {
        token: (1.0 + math.log(frequency)) * idf.get(token, 0.0)
        for token, frequency in counts.items()
        if frequency > 0 and token in idf
    }


def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    shared = set(left) & set(right)
    numerator = sum(left[token] * right[token] for token in shared)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    denominator = left_norm * right_norm
    return 0.0 if denominator <= 0.0 else numerator / denominator


def _positive_reciprocal_ranks(scores: Mapping[str, float]) -> dict[str, float]:
    ranked = sorted(
        (item for item in scores.items() if item[1] > 0.0),
        key=lambda item: (-item[1], item[0]),
    )
    return {candidate_id: 1.0 / rank for rank, (candidate_id, _) in enumerate(ranked, start=1)}


def _ndcg_at_10(relevances: Sequence[int]) -> float:
    observed = sum(
        (2.0**relevance - 1.0) / math.log2(index + 1.0)
        for index, relevance in enumerate(relevances[:10], start=1)
    )
    ideal = sum(
        (2.0**relevance - 1.0) / math.log2(index + 1.0)
        for index, relevance in enumerate(sorted(relevances, reverse=True)[:10], start=1)
    )
    return 1.0 if ideal <= 0.0 else observed / ideal


def _sigmoid(values: np.ndarray) -> np.ndarray:
    positive = values >= 0
    result = np.empty_like(values)
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    result[~positive] = exponent / (1.0 + exponent)
    return result


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _repository_macro_mean(values: Mapping[str, Sequence[float]]) -> float | None:
    if not values:
        return None
    return sum(sum(items) / len(items) for items in values.values()) / len(values)


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_PUBLIC_VECTOR_CHANNEL_ID",
    "PUBLIC_BOOTSTRAP_FEATURES",
    "PublicBootstrapMetrics",
    "PublicBootstrapProfile",
    "PublicFeatureRow",
    "PublicRelevanceCandidate",
    "PublicRelevanceDataset",
    "PublicRelevanceJudgment",
    "PublicRelevanceQuery",
    "build_public_feature_rows",
    "public_bootstrap_trainer_digest",
    "public_feature_rows_digest",
    "public_relevance_dataset_from_calibration",
    "train_public_bootstrap_profile",
    "train_public_relevance_profile",
]
