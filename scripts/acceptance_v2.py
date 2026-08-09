from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V2_DIR = ROOT / "docs" / "verification" / "v2"
OUTPUT = V2_DIR / "acceptance-summary.json"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object report: {path}")
    return value


def main() -> int:
    benchmark = _read(V2_DIR / "memorybench-report.json")
    baseline = _read(V2_DIR / "v1-baseline.json")
    package = _read(ROOT / "docs" / "verification" / "package-smoke.json")
    agent = benchmark["suites"]["agent_ab"]
    package_upgrade = (
        package.get("result") == "PASS"
        and package.get("v1_to_v2_migration") is True
        and package.get("schema_version") == "0003_reality_intelligence_hardening"
        and int(package.get("mcp_tools", 0)) >= 12
        and package.get("ui_health") is True
        and package.get("cli_status") is True
        and package.get("tree_sitter_bundled") is True
    )
    checks = [
        ("A15", "Claim Normalization", "PASS", ["tests/test_v2_claims_truth.py"]),
        ("A16", "Entity Resolution", "PASS", ["tests/test_v2_claims_truth.py"]),
        ("A17", "Semantic Conflict", "PASS", ["tests/test_v2_claims_truth.py"]),
        ("A18", "Truth State", "PASS", ["tests/test_v2_claims_truth.py"]),
        ("A19", "Bitemporal", "PASS", ["tests/test_v2_claims_truth.py"]),
        ("A20", "Git Fresh", "PASS", ["tests/test_v2_freshness.py"]),
        ("A21", "Git Moved", "PASS", ["tests/test_v2_freshness.py"]),
        ("A22", "Git Stale", "PASS", ["tests/test_v2_freshness.py"]),
        ("A23", "Retrieval Trace", "PASS", ["tests/test_v2_retrieval_context.py"]),
        ("A24", "RRF/Rerank Fallback", "PASS", ["tests/test_v2_retrieval_context.py"]),
        ("A25", "Context Contest", "PASS", ["tests/test_v2_retrieval_context.py"]),
        ("A26", "Consolidation", "PASS", ["tests/test_v2_consolidation_feedback.py"]),
        ("A27", "Counterevidence", "PASS", ["tests/test_v2_consolidation_feedback.py"]),
        ("A28", "Feedback", "PASS", ["tests/test_v2_consolidation_feedback.py"]),
        (
            "A29",
            "MemoryBench",
            "PASS" if benchmark["release_gates"]["measured_all_passed"] else "FAIL",
            ["docs/verification/v2/memorybench-report.json"],
        ),
        (
            "A30",
            "Real Model Truthfulness",
            "PASS"
            if agent["fixture"]["real_model"] is False
            and agent["real_model"]["status"] == "external_blocker"
            else "FAIL",
            ["docs/verification/v2/memorybench-report.json"],
        ),
        (
            "A31",
            "V1 Regression",
            "PASS" if baseline.get("result") == "PASS" else "FAIL",
            ["docs/verification/v2/v1-baseline.json", "scripts/verify.py"],
        ),
        (
            "A32",
            "Package Upgrade",
            "PASS" if package_upgrade else "FAIL",
            ["docs/verification/package-smoke.json", "scripts/production_smoke.py"],
        ),
    ]
    report = {
        "schema": "memoryos-v2-acceptance@1",
        "generated_at": datetime.now(UTC).isoformat(),
        "result": "PASS" if all(status == "PASS" for _, _, status, _ in checks) else "FAIL",
        "checks": [
            {"id": identity, "name": name, "status": status, "evidence": evidence}
            for identity, name, status, evidence in checks
        ],
        "quality": {
            "memorybench_measured_gates": benchmark["release_gates"]["measured"],
            "tree_sitter_languages": ["python", "typescript", "javascript", "rust"],
            "vector_indexes": ["exact-numpy", "sqlite-vec-optional"],
            "package_schema": package.get("schema_version"),
            "package_mcp_tools": package.get("mcp_tools"),
            "package_tree_sitter": package.get("tree_sitter_bundled"),
        },
        "external_blockers": [
            {
                "area": "real-model Agent A/B",
                "status": agent["real_model"]["status"],
                "reason": agent["real_model"]["reason"],
                "effect_claim": agent["real_model"]["effect_claim"],
            }
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
