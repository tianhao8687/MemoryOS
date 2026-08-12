from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryos.evaluation.model_review_report import (
    analyze_model_review,
    validate_model_review_bundle,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HUMAN_REVIEW_DATA = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "data"
CALIBRATION_DATA = REPOSITORY_ROOT / "benchmarks" / "calibration_v1" / "data"
MODEL_REVIEW = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "model_review"


def test_checked_in_model_review_is_provisional_reproducible_and_not_human_gold() -> None:
    report = json.loads((MODEL_REVIEW / "report.json").read_bytes())
    analyzed = analyze_model_review(
        dataset_root=HUMAN_REVIEW_DATA,
        calibration_root=CALIBRATION_DATA,
        review_a_path=MODEL_REVIEW / "reviewer-a2.responses.jsonl",
        review_b_path=MODEL_REVIEW / "reviewer-b.responses.jsonl",
        adjudication_path=MODEL_REVIEW / "adjudicated-provisional.jsonl",
    )

    assert report["status"] == analyzed["status"] == "model_adjudicated_provisional"
    assert report["human_gold_claim"] is analyzed["human_gold_claim"] is False
    assert report["total_rows"] == analyzed["total_rows"] == 1922
    assert report["core_disagreements_adjudicated"] == 527
    assert report["agreement"]["relevance_exact_rate"] == pytest.approx(0.7570239334)
    assert report["agreement"]["relevance_cohen_kappa"] == pytest.approx(0.2026306134)
    assert report["agreement"]["safety_cohen_kappa"] == pytest.approx(0.8341537122)
    assert report["adjudicated_distribution"]["semantic_relevance"] == {
        "0": 1608,
        "1": 178,
        "2": 92,
        "3": 44,
    }
    assert report["silver_diagnostic"]["reviewer_comparison"]["adjudicated"][
        "exact_relevance_rate"
    ] == pytest.approx(0.6098958333)
    assert report["silver_diagnostic"]["safety_by_silver_eligibility"] == {
        "cross_scope_guard": {"exclude": 60},
        "eligible": {"allow": 1755, "uncertain": 45},
        "future_guard": {"uncertain": 60},
    }
    assert report["artifacts"]["adjudicated"]["sha256"] == (
        "0c836306283ae750521b5526b844c8b0a0ef6c0a05137bcb5f846dcd558e83e1"
    )
    invalidated = report["protocol_audit"]["invalidated_attempts"]
    assert len(invalidated) == 1
    assert invalidated[0]["status"] == "invalidated"
    assert invalidated[0]["data_used_in_effective_review_or_adjudication"] is False
    assert all(report["build_checks"].values())

    validation = validate_model_review_bundle(
        dataset_root=HUMAN_REVIEW_DATA,
        calibration_root=CALIBRATION_DATA,
        model_review_root=MODEL_REVIEW,
    )
    assert validation["all_build_checks_passed"] is True
    assert validation["adjudication_sha256"] == report["artifacts"]["adjudicated"]["sha256"]
