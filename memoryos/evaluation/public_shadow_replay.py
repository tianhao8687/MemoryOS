from __future__ import annotations

import hashlib
import json
import math
import random
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import EmbeddingRow
from memoryos.domain.schemas import (
    CreatedBy,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import (
    PublicRelevanceDataset,
    PublicRelevanceQuery,
)
from memoryos.retrieval_v2.rrf_shadow import RRFChannelShadowProfile


def run_public_shadow_replay(
    dataset: PublicRelevanceDataset,
    profile: RRFChannelShadowProfile,
    *,
    output_path: Path,
    state_root: Path,
    embedding_base_url: str,
    embedding_model: str,
    split: CalibrationSplit = CalibrationSplit.TEST,
    queries_per_repository: int = 5,
    sample_seed: str = "memoryos-public-rrf-replay-v1",
) -> dict[str, Any]:
    if queries_per_repository < 1:
        raise ValueError("queries_per_repository must be positive")
    if output_path.exists():
        raise ValueError(f"refusing to overwrite public shadow replay: {output_path}")
    if dataset.dataset_sha256 != profile.source_dataset_sha256:
        raise ValueError("public replay dataset does not match the RRF shadow source")
    expected_prefix = f"fastembed:{embedding_model}@"
    if not profile.source_vector_channel_id.startswith(expected_prefix):
        raise ValueError("public replay embedding model does not match the RRF shadow source")
    live_identity = _verify_live_embedding(profile, embedding_base_url)
    selected_queries = select_replay_queries(
        dataset.queries[split],
        per_repository=queries_per_repository,
        seed=sample_seed,
    )
    candidates = {candidate.id: candidate for candidate in dataset.candidates}
    judgments = {
        (judgment.query_id, judgment.candidate_id): judgment
        for judgment in dataset.judgments[split]
        if judgment.eligible
    }
    state_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="rrf-replay-", dir=state_root) as temporary:
        temporary_root = Path(temporary)
        for index, query in enumerate(selected_queries, start=1):
            record = _run_query(
                query,
                profile,
                candidates=candidates,
                judgments=judgments,
                data_dir=temporary_root / hashlib.sha256(query.query_id.encode()).hexdigest()[:16],
                embedding_base_url=embedding_base_url,
                embedding_model=embedding_model,
            )
            records.append(record)
            print(
                json.dumps(
                    {
                        "completed": index,
                        "queries": len(selected_queries),
                        "query_id": query.query_id,
                        "required_rank_baseline": record["required_rank_baseline"],
                        "required_rank_shadow": record["required_rank_shadow"],
                    },
                    sort_keys=True,
                )
            )
    metrics = aggregate_replay_records(records)
    result = {
        "schema_version": "1.0",
        "status": "public_rrf_shadow_replay_complete",
        "production_eligible": False,
        "production_weights_changed": False,
        "split": split.value,
        "sample_seed": sample_seed,
        "queries_per_repository": queries_per_repository,
        "query_count": len(records),
        "repository_count": len({record["repository_id"] for record in records}),
        "dataset_sha256": dataset.dataset_sha256,
        "shadow_profile_sha256": profile.digest(),
        "source_public_profile_sha256": profile.source_public_profile_sha256,
        "channel_weights": profile.channel_weights,
        "embedding_identity": live_identity,
        "metrics": metrics,
        "decision": evaluate_public_shadow_gate(metrics),
        "records": records,
        "limitations": [
            "This replay uses public path-overlap labels, not downstream agent success.",
            "The public test partition is repository-held-out but not sealed.",
            "Runtime queries are capped at the production SearchRequest 1000-character limit.",
            "This diagnostic cannot authorize production activation.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def analyze_public_shadow_replay(
    source_path: Path,
    *,
    output_path: Path,
    bootstrap_rounds: int = 4_000,
    bootstrap_seed: int = 20_260_813,
) -> dict[str, Any]:
    if output_path.exists():
        raise ValueError(f"refusing to overwrite public shadow analysis: {output_path}")
    source_bytes = source_path.resolve(strict=True).read_bytes()
    try:
        source = json.loads(source_bytes)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid public shadow replay: {source_path}") from exc
    if not isinstance(source, dict):
        raise ValueError("public shadow replay must be a JSON object")
    if source.get("status") != "public_rrf_shadow_replay_complete":
        raise ValueError("public shadow replay is incomplete")
    if source.get("production_eligible") is not False:
        raise ValueError("public shadow replay must remain non-production")
    records = source.get("records")
    if (
        not isinstance(records, list)
        or not records
        or not all(isinstance(record, dict) for record in records)
    ):
        raise ValueError("public shadow replay records are missing or malformed")
    if source.get("query_count") != len(records):
        raise ValueError("public shadow replay query count does not match its records")
    repository_count = len({str(record["repository_id"]) for record in records})
    if source.get("repository_count") != repository_count:
        raise ValueError("public shadow replay repository count does not match its records")
    metrics = aggregate_replay_records(
        records,
        bootstrap_rounds=bootstrap_rounds,
        bootstrap_seed=bootstrap_seed,
    )
    result = {
        "schema_version": "1.0",
        "status": "public_rrf_shadow_replay_analysis_complete",
        "production_eligible": False,
        "production_weights_changed": False,
        "source_report_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "dataset_sha256": source.get("dataset_sha256"),
        "shadow_profile_sha256": source.get("shadow_profile_sha256"),
        "source_public_profile_sha256": source.get("source_public_profile_sha256"),
        "split": source.get("split"),
        "sample_seed": source.get("sample_seed"),
        "queries_per_repository": source.get("queries_per_repository"),
        "query_count": len(records),
        "repository_count": repository_count,
        "metrics": metrics,
        "decision": evaluate_public_shadow_gate(metrics),
        "limitations": [
            "Confidence intervals quantify sampling uncertainty in this fixed public replay only.",
            "The public path-overlap labels are not downstream agent-success ground truth.",
            "This diagnostic cannot authorize production activation.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def evaluate_public_shadow_gate(
    metrics: Mapping[str, Any],
    *,
    minimum_repositories: int = 3,
) -> dict[str, Any]:
    if minimum_repositories < 1:
        raise ValueError("public shadow gate requires at least one repository")
    per_repository = metrics["per_repository"]
    bootstrap = metrics["paired_bootstrap"]
    repository_count = len(per_repository)
    ndcg_ci95_low = float(bootstrap["ndcg_at_10_delta"]["ci95_low"])
    recall_delta = float(metrics["delta"]["required_recall_at_5"])
    worst_ndcg_delta = min(
        float(summary["delta"]["ndcg_at_10"]) for summary in per_repository.values()
    )
    worst_recall_delta = min(
        float(summary["delta"]["required_recall_at_5"]) for summary in per_repository.values()
    )
    gates: dict[str, dict[str, object]] = {
        "minimum_repository_coverage": {
            "passed": repository_count >= minimum_repositories,
            "observed": repository_count,
            "required": minimum_repositories,
        },
        "ndcg_ci95_lower_bound_positive": {
            "passed": ndcg_ci95_low > 0.0,
            "observed": ndcg_ci95_low,
            "required": "> 0",
        },
        "overall_required_recall_non_regression": {
            "passed": recall_delta >= 0.0,
            "observed": recall_delta,
            "required": ">= 0",
        },
        "worst_repository_ndcg_non_regression": {
            "passed": worst_ndcg_delta >= 0.0,
            "observed": worst_ndcg_delta,
            "required": ">= 0",
        },
        "worst_repository_required_recall_non_regression": {
            "passed": worst_recall_delta >= 0.0,
            "observed": worst_recall_delta,
            "required": ">= 0",
        },
    }
    failed = [name for name, gate in gates.items() if not bool(gate["passed"])]
    return {
        "policy": "public_rrf_shadow_gate_v1",
        "status": "shadow_gate_passed" if not failed else "shadow_gate_failed",
        "recommendation": (
            "advance_to_causal_shadow_only" if not failed else "retain_frozen_baseline"
        ),
        "production_eligible": False,
        "gates": gates,
        "failed_gates": failed,
    }


def select_replay_queries(
    queries: Sequence[PublicRelevanceQuery],
    *,
    per_repository: int,
    seed: str,
) -> list[PublicRelevanceQuery]:
    if per_repository < 1 or not seed:
        raise ValueError("public replay sampling requires a positive count and non-empty seed")
    by_repository: dict[str, list[PublicRelevanceQuery]] = defaultdict(list)
    for query in queries:
        by_repository[query.repository_id].append(query)
    selected: list[PublicRelevanceQuery] = []
    for repository_id, repository_queries in sorted(by_repository.items()):
        ordered = sorted(
            repository_queries,
            key=lambda query: hashlib.sha256(
                f"{seed}:{repository_id}:{query.query_id}".encode()
            ).hexdigest(),
        )
        selected.extend(ordered[:per_repository])
    return selected


def rank_metrics(
    ranking: Sequence[str],
    relevances: Mapping[str, int],
    required: set[str],
) -> dict[str, float | int | None]:
    ranked_relevances = [relevances[candidate_id] for candidate_id in ranking]
    observed = sum(
        (2.0**relevance - 1.0) / math.log2(index + 1.0)
        for index, relevance in enumerate(ranked_relevances[:10], start=1)
    )
    ideal = sum(
        (2.0**relevance - 1.0) / math.log2(index + 1.0)
        for index, relevance in enumerate(
            sorted(ranked_relevances, reverse=True)[:10],
            start=1,
        )
    )
    required_rank = next(
        (index for index, candidate_id in enumerate(ranking, start=1) if candidate_id in required),
        None,
    )
    return {
        "ndcg_at_10": 1.0 if ideal <= 0.0 else observed / ideal,
        "required_recall_at_5": float(bool(required & set(ranking[:5]))),
        "required_rank": required_rank,
    }


def _run_query(
    query: PublicRelevanceQuery,
    profile: RRFChannelShadowProfile,
    *,
    candidates: Mapping[str, Any],
    judgments: Mapping[tuple[str, str], Any],
    data_dir: Path,
    embedding_base_url: str,
    embedding_model: str,
) -> dict[str, Any]:
    settings = settings_for(
        data_dir,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        ann_enabled=False,
    )
    database = Database(settings)
    database.initialize()
    baseline = MemoryService(database, settings)
    shadow: MemoryService | None = None
    memory_to_candidate: dict[str, str] = {}
    try:
        for candidate_id in query.candidate_ids:
            candidate = candidates[candidate_id]
            content = str(candidate.text)
            created = baseline.propose(
                MemoryCreate(
                    scope_type=ScopeType.REPOSITORY,
                    scope_key=query.repository_id,
                    memory_type=MemoryType.PROJECT,
                    category="public-retrieval-replay",
                    key=f"public-replay.{candidate_id}",
                    title=candidate_id,
                    content=content,
                    confidence=0.5,
                    importance=0.5,
                    created_by=CreatedBy.MANUAL,
                    source=SourceCreate(
                        source_type=SourceType.IMPORT,
                        source_ref=f"swe-gym:{candidate_id}",
                        captured_at=datetime.now(UTC),
                        excerpt=content[:10_000],
                        metadata={"public_replay": True},
                    ),
                    activate_immediately=True,
                    metadata={"public_candidate_id": candidate_id},
                ),
                actor="public-shadow-replay",
            )
            memory_to_candidate[str(created["id"])] = candidate_id
        with database.session() as session:
            embedding_count = int(
                session.scalar(select(func.count()).select_from(EmbeddingRow)) or 0
            )
        if embedding_count != len(query.candidate_ids):
            raise RuntimeError("public shadow replay did not index every candidate")
        request = SearchRequest(
            query=query.query[:1000],
            scope_type=ScopeType.REPOSITORY,
            scope_key=query.repository_id,
            limit=len(query.candidate_ids),
        )
        baseline_result = baseline.search(request)
        shadow = MemoryService(database, settings, retrieval_rrf_channel_profile=profile)
        shadow_result = shadow.search(request)
        baseline_ranking = _candidate_ranking(baseline_result, memory_to_candidate)
        shadow_ranking = _candidate_ranking(shadow_result, memory_to_candidate)
        expected = set(query.candidate_ids)
        if set(baseline_ranking) != expected or set(shadow_ranking) != expected:
            raise RuntimeError("public shadow replay did not rank the complete candidate pool")
        query_judgments = {
            candidate_id: judgments[(query.query_id, candidate_id)]
            for candidate_id in query.candidate_ids
        }
        relevances = {
            candidate_id: int(judgment.relevance)
            for candidate_id, judgment in query_judgments.items()
        }
        required = {
            candidate_id for candidate_id, value in query_judgments.items() if value.required
        }
        baseline_metrics = rank_metrics(baseline_ranking, relevances, required)
        shadow_metrics = rank_metrics(shadow_ranking, relevances, required)
        return {
            "query_id": query.query_id,
            "repository_id": query.repository_id,
            "candidate_count": len(query.candidate_ids),
            "query_truncated": len(query.query) > 1000,
            "ranking_changed": baseline_ranking != shadow_ranking,
            "top_1_changed": baseline_ranking[:1] != shadow_ranking[:1],
            "top_5_changed": baseline_ranking[:5] != shadow_ranking[:5],
            "ndcg_at_10_baseline": baseline_metrics["ndcg_at_10"],
            "ndcg_at_10_shadow": shadow_metrics["ndcg_at_10"],
            "required_recall_at_5_baseline": baseline_metrics["required_recall_at_5"],
            "required_recall_at_5_shadow": shadow_metrics["required_recall_at_5"],
            "required_rank_baseline": baseline_metrics["required_rank"],
            "required_rank_shadow": shadow_metrics["required_rank"],
            "baseline_config_hash": str(baseline_result["config_hash"]),
            "shadow_config_hash": str(shadow_result["config_hash"]),
        }
    finally:
        if shadow is not None:
            shadow.close()
        baseline.close()
        database.close()


def _candidate_ranking(result: Mapping[str, Any], mapping: Mapping[str, str]) -> list[str]:
    return [mapping[str(item["memory"]["id"])] for item in result["items"]]


def _verify_live_embedding(
    profile: RRFChannelShadowProfile,
    base_url: str,
) -> dict[str, Any]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=10.0)
        response.raise_for_status()
        health = response.json()
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise RuntimeError("public shadow replay embedding health check failed") from exc
    expected = {
        "vector_channel_id": profile.source_vector_channel_id,
        "vector_channel_source_sha256": profile.source_vector_channel_sha256,
        "vector_feature_adapter_sha256": profile.source_vector_adapter_sha256,
    }
    identity_mismatch = isinstance(health, dict) and any(
        health.get(key) != value for key, value in expected.items()
    )
    if not isinstance(health, dict) or identity_mismatch:
        raise RuntimeError("public shadow replay embedding identity mismatch")
    return {
        **expected,
        "model": health.get("model"),
        "dimensions": health.get("dimensions"),
    }


def aggregate_replay_records(
    records: Sequence[Mapping[str, Any]],
    *,
    bootstrap_rounds: int = 4_000,
    bootstrap_seed: int = 20_260_813,
) -> dict[str, Any]:
    if not records:
        raise ValueError("public shadow replay has no records")
    if bootstrap_rounds < 100:
        raise ValueError("public shadow replay requires at least 100 bootstrap rounds")
    by_repository: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        by_repository[str(record["repository_id"])].append(record)

    def macro(field: str) -> float:
        repository_means = [
            sum(float(record[field]) for record in repository_records) / len(repository_records)
            for repository_records in by_repository.values()
        ]
        return sum(repository_means) / len(repository_means)

    baseline_ndcg = macro("ndcg_at_10_baseline")
    shadow_ndcg = macro("ndcg_at_10_shadow")
    baseline_recall = macro("required_recall_at_5_baseline")
    shadow_recall = macro("required_recall_at_5_shadow")
    per_repository = {
        repository_id: _repository_summary(repository_records)
        for repository_id, repository_records in sorted(by_repository.items())
    }
    return {
        "metric_aggregation": "repository_macro_average",
        "baseline": {
            "ndcg_at_10": baseline_ndcg,
            "required_recall_at_5": baseline_recall,
        },
        "shadow": {
            "ndcg_at_10": shadow_ndcg,
            "required_recall_at_5": shadow_recall,
        },
        "delta": {
            "ndcg_at_10": shadow_ndcg - baseline_ndcg,
            "required_recall_at_5": shadow_recall - baseline_recall,
        },
        "paired_bootstrap": {
            "method": "stratified_by_repository_resample_queries_with_replacement",
            "rounds": bootstrap_rounds,
            "seed": bootstrap_seed,
            "ndcg_at_10_delta": _stratified_paired_bootstrap(
                by_repository,
                baseline_field="ndcg_at_10_baseline",
                shadow_field="ndcg_at_10_shadow",
                rounds=bootstrap_rounds,
                seed=bootstrap_seed,
            ),
            "required_recall_at_5_delta": _stratified_paired_bootstrap(
                by_repository,
                baseline_field="required_recall_at_5_baseline",
                shadow_field="required_recall_at_5_shadow",
                rounds=bootstrap_rounds,
                seed=bootstrap_seed + 1,
            ),
        },
        "per_repository": per_repository,
        "ranking_changed_queries": sum(bool(record["ranking_changed"]) for record in records),
        "top_1_changed_queries": sum(bool(record["top_1_changed"]) for record in records),
        "top_5_changed_queries": sum(bool(record["top_5_changed"]) for record in records),
        "required_rank_improved_queries": sum(_rank_direction(record) < 0 for record in records),
        "required_rank_worsened_queries": sum(_rank_direction(record) > 0 for record in records),
        "required_rank_unchanged_queries": sum(_rank_direction(record) == 0 for record in records),
    }


def _repository_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(records)

    def mean(field: str) -> float:
        return sum(float(record[field]) for record in records) / count

    baseline_ndcg = mean("ndcg_at_10_baseline")
    shadow_ndcg = mean("ndcg_at_10_shadow")
    baseline_recall = mean("required_recall_at_5_baseline")
    shadow_recall = mean("required_recall_at_5_shadow")
    return {
        "query_count": count,
        "baseline": {
            "ndcg_at_10": baseline_ndcg,
            "required_recall_at_5": baseline_recall,
        },
        "shadow": {
            "ndcg_at_10": shadow_ndcg,
            "required_recall_at_5": shadow_recall,
        },
        "delta": {
            "ndcg_at_10": shadow_ndcg - baseline_ndcg,
            "required_recall_at_5": shadow_recall - baseline_recall,
        },
        "required_rank_improved_queries": sum(_rank_direction(record) < 0 for record in records),
        "required_rank_worsened_queries": sum(_rank_direction(record) > 0 for record in records),
        "required_rank_unchanged_queries": sum(_rank_direction(record) == 0 for record in records),
    }


def _stratified_paired_bootstrap(
    by_repository: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    baseline_field: str,
    shadow_field: str,
    rounds: int,
    seed: int,
) -> dict[str, float]:
    rng = random.Random(seed)  # noqa: S311 - reproducible bootstrap, not cryptography
    simulated: list[float] = []
    observed_by_repository: list[float] = []
    repositories = [records for _, records in sorted(by_repository.items())]
    for records in repositories:
        observed_by_repository.append(
            sum(float(record[shadow_field]) - float(record[baseline_field]) for record in records)
            / len(records)
        )
    for _ in range(rounds):
        repository_means: list[float] = []
        for records in repositories:
            differences = [
                float(record[shadow_field]) - float(record[baseline_field]) for record in records
            ]
            repository_means.append(
                sum(differences[rng.randrange(len(differences))] for _ in differences)
                / len(differences)
            )
        simulated.append(sum(repository_means) / len(repository_means))
    simulated.sort()
    return {
        "difference": sum(observed_by_repository) / len(observed_by_repository),
        "ci95_low": simulated[int(rounds * 0.025)],
        "ci95_high": simulated[min(rounds - 1, int(rounds * 0.975))],
        "probability_improvement": sum(value > 0.0 for value in simulated) / rounds,
    }


def _rank_direction(record: Mapping[str, Any]) -> int:
    baseline = record.get("required_rank_baseline")
    shadow = record.get("required_rank_shadow")
    if baseline is None or shadow is None:
        return 0
    return (int(shadow) > int(baseline)) - (int(shadow) < int(baseline))


__all__ = [
    "aggregate_replay_records",
    "analyze_public_shadow_replay",
    "evaluate_public_shadow_gate",
    "rank_metrics",
    "run_public_shadow_replay",
    "select_replay_queries",
]
