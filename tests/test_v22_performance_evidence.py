from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memoryos.evaluation.evidence_hashing import canonical_file_sha256

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "verification" / "v2.2" / "performance-tiers.json"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _verified_report(relative_path: str, expected_hash: str) -> dict[str, Any]:
    path = ROOT / relative_path
    assert canonical_file_sha256(path) == expected_hash
    return _read(path)


def test_performance_evidence_hash_is_stable_across_checkout_line_endings(
    tmp_path: Path,
) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "ok": true\n}\n')
    crlf.write_bytes(b'{\r\n  "ok": true\r\n}\r\n')

    assert canonical_file_sha256(lf) == canonical_file_sha256(crlf)


def test_performance_tier_index_matches_hashed_evidence_and_executed_channels() -> None:
    index = _read(INDEX)
    assert index["file_sha256_policy"] == "utf8-text-lf-normalized-v1"
    tier_1 = index["tiers"]["tier_1"]
    core = _verified_report(tier_1["path"], tier_1["file_sha256"])
    assert core["label"] == "100K FTS-first Core Pipeline"
    assert core["environment"]["channels"]["executed"] == ["fts"]
    assert core["environment"]["channels"]["contributing"] == ["fts"]
    assert core["environment"]["record_counts"] == {
        "memories": 100_000,
        "embeddings": 0,
        "claims": 0,
        "claim_versions": 0,
        "relations": 0,
    }

    tier_2 = index["tiers"]["tier_2"]
    for size in (10_000, 20_000):
        entry = tier_2["results"][str(size)]
        hybrid = _verified_report(entry["path"], entry["file_sha256"])
        assert hybrid["passed"] is True
        assert hybrid["record_counts"]["memories"] == size
        assert hybrid["record_counts"]["embeddings"] == size
        assert hybrid["record_counts"]["claims"] == size
        assert hybrid["record_counts"]["claim_versions"] == 0
        assert hybrid["provider"]["fixture"] is False
        for mode in ("sqlite_vec_ann", "exact_fallback"):
            assert hybrid["modes"][mode]["channels"]["contributing"] == [
                "fts",
                "graph",
                "temporal",
                "vector",
            ]
            assert hybrid["modes"][mode]["channels"]["degraded"] == []

    assert index["tiers"]["tier_3"]["status"] == "not_executed"
    assert index["tiers"]["tier_3"]["search_p95_ms"] is None

    current_truth = index["current_truth"]
    truth_report = _verified_report(
        current_truth["path"],
        current_truth["file_sha256"],
    )
    assert truth_report["gates"]["constant_query_bound"] is True
    assert truth_report["measurements"]["1000"]["sql_queries"]["max"] == 9
