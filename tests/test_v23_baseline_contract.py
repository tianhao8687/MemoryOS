from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from memoryos.context.token_meter import UnicodeHeuristicTokenCounter, canonical_json
from scripts.capture_v22_context_golden import BASELINE_COMMIT, build_fixture

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "docs" / "verification" / "v2.3" / "v22-context-compiler-golden.json"


def _load() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.v23
def test_v22_context_golden_is_reproducible_from_the_frozen_fixture_state() -> None:
    golden = _load()

    assert golden == build_fixture()
    assert golden["baseline_commit"] == BASELINE_COMMIT
    assert golden["budget_contract"] == {
        "field": "budget",
        "scope": "legacy text only",
        "token_semantics": False,
        "unit": "characters",
    }


@pytest.mark.v23
def test_v22_context_response_and_retrieval_run_contract_are_fully_frozen() -> None:
    golden = _load()
    response = golden["context_response"]

    assert set(response) == {
        "task",
        "repository",
        "branch",
        "budget",
        "characters_used",
        "budget_exceeded",
        "retrieval_mode",
        "retrieval_run_id",
        "query_plan",
        "truth_state",
        "sections",
        "manifest",
        "text",
        "debug",
    }
    assert response["characters_used"] == len(response["text"])
    assert response["budget_exceeded"] is False
    assert golden["canonical_mcp_tool_result"] == {"ok": True, "result": response}
    run = golden["retrieval_run"]
    assert set(run) == {
        "id",
        "query",
        "task",
        "scope",
        "selected_memory_ids",
        "candidate_features",
        "context_manifest",
        "config_hash",
        "created_at",
    }


@pytest.mark.v23
def test_v22_golden_covers_truth_freshness_constraint_and_source_grounding() -> None:
    golden = _load()
    manifest = golden["context_response"]["manifest"]
    by_id = {item["memory_id"]: item for item in manifest}

    assert by_id["m_resolved"]["truth_state"] == "resolved"
    assert by_id["m_contested_left"]["truth_state"] == "contested"
    assert by_id["m_contested_right"]["truth_state"] == "contested"
    assert by_id["m_suspect"]["freshness"] == "suspect"
    assert by_id["m_stale"]["freshness"] == "stale"
    assert by_id["m_source_grounded"]["claim_ids"] == []
    text = golden["context_response"]["text"]
    assert "Timeout must not exceed 30 seconds except offline migration jobs." in text
    assert set(golden["coverage_cases"]) == {
        "resolved",
        "contested",
        "suspect",
        "stale",
        "constraint",
        "source_grounded",
    }


@pytest.mark.v23
def test_golden_payload_breakdown_counts_complete_canonical_tool_result() -> None:
    golden = _load()
    response = golden["context_response"]
    breakdown = golden["payload_size_breakdown"]
    counter = UnicodeHeuristicTokenCounter()

    values = {
        "text": str(response["text"]),
        "sections": canonical_json(response["sections"]),
        "manifest": canonical_json(response["manifest"]),
        "debug": canonical_json(response["debug"]),
        "context_response_total": canonical_json(response),
        "mcp_tool_result_total": canonical_json(golden["canonical_mcp_tool_result"]),
    }
    for name, serialized in values.items():
        assert breakdown[name] == {
            "characters": len(serialized),
            "utf8_bytes": len(serialized.encode("utf-8")),
            "estimated_tokens": counter.count_text(serialized),
        }
    assert (
        breakdown["mcp_tool_result_total"]["estimated_tokens"]
        > breakdown["text"]["estimated_tokens"]
    )


def test_golden_references_immutable_real_workload_evidence_and_uses_lf() -> None:
    golden = _load()
    report = golden["real_workload_report"]
    report_path = ROOT / report["path"]

    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == report["sha256"]
    assert report["effect_claim"] == "none"
    payload = GOLDEN_PATH.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r" not in payload
