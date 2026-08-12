from __future__ import annotations

import json
from pathlib import Path

from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import PublicRelevanceQuery
from memoryos.evaluation.public_shadow_replay import (
    aggregate_replay_records,
    analyze_public_shadow_replay,
    evaluate_public_shadow_gate,
    rank_metrics,
    select_replay_queries,
)


def _query(query_id: str, repository_id: str) -> PublicRelevanceQuery:
    return PublicRelevanceQuery(
        query_id=query_id,
        repository_id=repository_id,
        split=CalibrationSplit.TEST,
        query="repair parser",
        candidate_ids=("a", "b"),
    )


def test_replay_sampling_is_repository_balanced_and_deterministic() -> None:
    queries = [
        *(_query(f"a-{index}", "a/repo") for index in range(5)),
        *(_query(f"b-{index}", "b/repo") for index in range(4)),
    ]
    first = select_replay_queries(queries, per_repository=2, seed="fixed")
    second = select_replay_queries(list(reversed(queries)), per_repository=2, seed="fixed")

    assert first == second
    assert len(first) == 4
    assert {query.repository_id for query in first} == {"a/repo", "b/repo"}


def test_rank_metrics_measure_ndcg_and_required_recall() -> None:
    relevances = {"a": 0, "b": 3, "c": 1}
    good = rank_metrics(["b", "c", "a"], relevances, {"b"})
    bad = rank_metrics(["a", "c", "b"], relevances, {"b"})

    assert good["ndcg_at_10"] == 1.0
    assert float(good["ndcg_at_10"]) > float(bad["ndcg_at_10"])
    assert good["required_recall_at_5"] == 1.0
    assert bad["required_rank"] == 3


def test_replay_aggregation_is_repository_macro_and_bootstrap_deterministic() -> None:
    records = [
        _record("a/repo", baseline=0.0, shadow=1.0, baseline_rank=10, shadow_rank=1),
        _record("a/repo", baseline=1.0, shadow=1.0, baseline_rank=1, shadow_rank=1),
        _record("b/repo", baseline=1.0, shadow=0.0, baseline_rank=1, shadow_rank=10),
        _record("b/repo", baseline=0.0, shadow=0.0, baseline_rank=None, shadow_rank=None),
    ]

    first = aggregate_replay_records(records, bootstrap_rounds=100, bootstrap_seed=7)
    second = aggregate_replay_records(records, bootstrap_rounds=100, bootstrap_seed=7)

    assert first == second
    assert first["delta"]["ndcg_at_10"] == 0.0
    assert first["paired_bootstrap"]["ndcg_at_10_delta"]["difference"] == 0.0
    assert first["per_repository"]["a/repo"]["delta"]["ndcg_at_10"] == 0.5
    assert first["per_repository"]["b/repo"]["delta"]["ndcg_at_10"] == -0.5
    assert first["required_rank_improved_queries"] == 1
    assert first["required_rank_worsened_queries"] == 1
    assert first["required_rank_unchanged_queries"] == 2


def test_completed_replay_can_be_analyzed_without_rerunning(tmp_path: Path) -> None:
    records = [
        _record("a/repo", baseline=0.0, shadow=1.0, baseline_rank=10, shadow_rank=1),
        _record("b/repo", baseline=1.0, shadow=0.0, baseline_rank=1, shadow_rank=10),
    ]
    source = tmp_path / "replay.json"
    output = tmp_path / "analysis.json"
    source.write_text(
        json.dumps(
            {
                "status": "public_rrf_shadow_replay_complete",
                "production_eligible": False,
                "query_count": 2,
                "repository_count": 2,
                "records": records,
            }
        ),
        encoding="utf-8",
    )

    result = analyze_public_shadow_replay(
        source,
        output_path=output,
        bootstrap_rounds=100,
        bootstrap_seed=7,
    )

    assert result["status"] == "public_rrf_shadow_replay_analysis_complete"
    assert result["production_eligible"] is False
    assert result["query_count"] == 2
    assert result["decision"]["recommendation"] == "retain_frozen_baseline"
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_public_shadow_gate_can_only_advance_to_causal_shadow() -> None:
    records = [
        _record(repository, baseline=0.0, shadow=1.0, baseline_rank=10, shadow_rank=1)
        for repository in ("a/repo", "b/repo", "c/repo")
        for _ in range(3)
    ]
    metrics = aggregate_replay_records(records, bootstrap_rounds=100, bootstrap_seed=7)

    decision = evaluate_public_shadow_gate(metrics)

    assert decision["status"] == "shadow_gate_passed"
    assert decision["recommendation"] == "advance_to_causal_shadow_only"
    assert decision["production_eligible"] is False
    assert decision["failed_gates"] == []


def _record(
    repository_id: str,
    *,
    baseline: float,
    shadow: float,
    baseline_rank: int | None,
    shadow_rank: int | None,
) -> dict[str, object]:
    return {
        "repository_id": repository_id,
        "ndcg_at_10_baseline": baseline,
        "ndcg_at_10_shadow": shadow,
        "required_recall_at_5_baseline": baseline,
        "required_recall_at_5_shadow": shadow,
        "required_rank_baseline": baseline_rank,
        "required_rank_shadow": shadow_rank,
        "ranking_changed": baseline != shadow,
        "top_1_changed": baseline != shadow,
        "top_5_changed": baseline != shadow,
    }
