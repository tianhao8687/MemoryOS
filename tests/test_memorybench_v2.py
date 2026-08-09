from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryos.evaluation.memorybench import (
    DATASET_PATH,
    MemoryBenchV2,
    _suite_performance_100k,
)


@pytest.mark.v2
def test_a29_memorybench_runs_frozen_quality_suites(tmp_path: Path) -> None:
    rows = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 100
    assert len({row["id"] for row in rows}) == 100

    runner = MemoryBenchV2()
    report = runner.run(include_performance=False)

    expected_sizes = {
        "extraction": 100,
        "retrieval": 250,
        "conflict": 200,
        "temporal": 120,
        "git_freshness": 120,
        "consolidation": 80,
        "context": 150,
        "agent_ab": 30,
    }
    assert {
        name: suite["sample_size"] for name, suite in report["suites"].items()
    } == expected_sizes
    assert report["release_gates"]["measured_all_passed"] is True
    assert len(report["config_hash"]) == 64

    paths = runner.write(report, tmp_path)
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["seed"] == report["seed"]
    assert "Fixture Agent A/B validates the harness only" in paths["html"].read_text(
        encoding="utf-8"
    )


@pytest.mark.v2
def test_a30_fixture_agent_ab_is_not_reported_as_real_model_evidence() -> None:
    report = MemoryBenchV2().run(include_performance=False)
    agent = report["suites"]["agent_ab"]

    assert agent["fixture"]["sample_size"] >= 30
    assert agent["fixture"]["real_model"] is False
    assert agent["fixture"]["claim"] == "harness_validation_only"
    assert agent["real_model"]["status"] == "external_blocker"
    assert agent["real_model"]["effect_claim"] == "not_evaluated"
    assert agent["truthfulness_gate"]["passed"] is True
    assert "ci95_low" in agent["fixture"]["task_success_difference"]


@pytest.mark.v2
@pytest.mark.slow
def test_a32_measured_100k_search_p95_is_recorded() -> None:
    performance = _suite_performance_100k()

    assert performance["sample_size"] == 100_000
    assert performance["evidence_type"] == "measured-local-machine"
    assert performance["reranker"] == "disabled"
    assert performance["v2"]["p95_ms"] > 0
    assert performance["gate"]["passed"] is True
