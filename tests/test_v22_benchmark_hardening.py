from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import RetrievalRunRow
from memoryos.evaluation import ProductionCodingMemoryBench
from memoryos.retrieval_v2 import RetrievalPipeline


def test_production_coding_bench_executes_real_services_with_isolated_gold(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "production-bench-data"
    report = ProductionCodingMemoryBench().run(data_dir)

    assert report["evidence_type"] == "local_production_path_integration"
    assert report["effect_claim"] == "integration_correctness_only"
    assert report["production_path_executed"] is True
    assert report["blind_protocol"]["runtime_payload_contains_gold"] is False
    assert report["sample_sizes"] == {
        "retrieval": 4,
        "context": 1,
        "temporal": 7,
        "conflict": 2,
    }
    assert report["retrieval"]["recall_at_5"] == 1.0
    assert report["context"]["target_inclusion_rate"] == 1.0
    assert report["temporal"]["accuracy"] == 1.0
    assert report["conflict"]["accuracy"] == 1.0
    assert report["retrieval"]["contributing_channels"] == ["fts"]
    assert "vector" in report["retrieval"]["degraded_channels"]
    assert "memoryos.context.compiler.TaskAwareContextCompiler" in report["active_modules"]
    assert report["provenance"]["repository_commit"] != "unavailable"
    assert len(report["provenance"]["retrieval_config_hash"]) == 64
    assert all(report["gates"].values())

    database = Database(settings_for(data_dir))
    try:
        with database.session() as session:
            count = int(session.scalar(select(func.count()).select_from(RetrievalRunRow)) or 0)
        assert count == (report["sample_sizes"]["retrieval"] + report["sample_sizes"]["context"])
    finally:
        database.close()


def test_production_coding_bench_fails_when_retrieval_pipeline_is_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_search(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("intentional retrieval pipeline break")

    monkeypatch.setattr(RetrievalPipeline, "search", broken_search)

    with pytest.raises(RuntimeError, match="intentional retrieval pipeline break"):
        ProductionCodingMemoryBench().run(tmp_path / "broken-production-bench")
