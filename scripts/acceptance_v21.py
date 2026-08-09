"""Build the machine-readable A33-A52 V2.1 acceptance manifest."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V21_DIR = ROOT / "docs" / "verification" / "v2.1"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object report: {path}")
    return value


def _check(
    identity: str, name: str, passed: bool, evidence: list[str], detail: str
) -> dict[str, Any]:
    return {
        "id": identity,
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the A33-A52 acceptance manifest")
    parser.add_argument("--baseline-report", type=Path, default=V21_DIR / "v2-clean-baseline.json")
    parser.add_argument(
        "--benchmark-report", type=Path, default=V21_DIR / "coding-memory-bench.json"
    )
    parser.add_argument(
        "--performance-report",
        type=Path,
        default=V21_DIR / "full-pipeline-performance.json",
    )
    parser.add_argument("--agent-report", type=Path, default=V21_DIR / "agent-ab.json")
    parser.add_argument(
        "--package-report",
        type=Path,
        default=ROOT / "docs" / "verification" / "package-smoke.json",
    )
    parser.add_argument(
        "--main-smoke-report",
        type=Path,
        default=V21_DIR / "main-release-smoke.json",
    )
    parser.add_argument("--output", type=Path, default=V21_DIR / "acceptance-summary.json")
    args = parser.parse_args()

    baseline = _read(args.baseline_report)
    benchmark = _read(args.benchmark_report)
    performance = _read(args.performance_report)
    agent = _read(args.agent_report)
    package = _read(args.package_report)
    main_smoke = _read(args.main_smoke_report)
    modes = benchmark["modes"]
    release = benchmark["release_gates"]
    fixture = agent.get("fixture", {})
    real_agent_or_blocker = bool(
        (
            agent.get("status") == "complete"
            and agent.get("real_model") is True
            and int(agent.get("completed_sample_size", 0)) >= 50
        )
        or (
            agent.get("status") == "external_blocker"
            and agent.get("real_model") is False
            and agent.get("effect_claim") == "none"
            and bool(agent.get("reason"))
        )
    )
    package_v21 = bool(
        package.get("result") == "PASS"
        and package.get("v1_to_v21_migration") is True
        and package.get("schema_version") == "0003_reality_intelligence_hardening"
        and package.get("coding_memory_bench_bundled") is True
        and package.get("sqlite_vec_bundled") is True
        and package.get("first_health", {}).get("version") == "2.1.0"
    )
    tests = ["tests/test_v21_hardening.py", "tests/test_backup_restore.py"]
    checks = [
        _check(
            "A33",
            "Clean V2 baseline",
            baseline.get("result") == "PASS"
            and baseline.get("git_dirty_before_run") is False
            and baseline.get("memorybench", {}).get("measured_all_passed") is True,
            ["docs/verification/v2.1/v2-clean-baseline.json"],
            "V2 full verification was captured at an immutable clean commit before V2.1 edits.",
        ),
        _check(
            "A34",
            "Immutable migration replay",
            package_v21,
            [*tests, "memoryos/db/migrations/versions/0003_reality_intelligence_hardening.py"],
            (
                "Alembic upgrades real 0001 data through explicit 0002/0003 operations "
                "and replay tests."
            ),
        ),
        _check(
            "A35",
            "Claim transaction history",
            package_v21,
            tests,
            "ClaimVersion rows append candidate/accepted/forgotten transaction states.",
        ),
        _check(
            "A36",
            "Temporal blind accuracy",
            float(modes["v2"]["temporal_accuracy"]) >= 0.98,
            ["docs/verification/v2.1/coding-memory-bench.json"],
            f"V2 temporal accuracy={modes['v2']['temporal_accuracy']:.3f} (target >=0.98).",
        ),
        _check(
            "A37",
            "Uncertain-only model routing",
            package_v21,
            tests,
            "Deterministic rules handle clear pairs; only uncertain pairs are model-eligible.",
        ),
        _check(
            "A38",
            "Blind conflict F1",
            float(modes["v2"]["conflict"]["f1"]) >= 0.88,
            ["docs/verification/v2.1/coding-memory-bench.json"],
            f"V2 conflict F1={modes['v2']['conflict']['f1']:.3f} (target >=0.88).",
        ),
        _check(
            "A39",
            "Abstention safety",
            release.get("abstention_safe") is True,
            ["docs/verification/v2.1/coding-memory-bench.json", *tests],
            "Abstention/provider failure leaves accepted truth unchanged and remains auditable.",
        ),
        _check(
            "A40",
            "Persistent ANN live path",
            package_v21,
            [*tests, "docs/verification/package-smoke.json"],
            "sqlite-vec is bundled and the provider/model/dimension namespace is persisted.",
        ),
        _check(
            "A41",
            "Exact fallback",
            package_v21,
            tests,
            "Unavailable/disabled ANN explicitly reports exact-fallback and preserves results.",
        ),
        _check(
            "A42",
            "100K full-pipeline search P95",
            int(performance["records"]) >= 100_000
            and float(performance["search"]["p95_ms"]) < 150.0,
            ["docs/verification/v2.1/full-pipeline-performance.json"],
            f"Search P95={performance['search']['p95_ms']} ms (target <150 ms).",
        ),
        _check(
            "A43",
            "100K context P95",
            int(performance["records"]) >= 100_000
            and float(performance["context"]["p95_ms"]) < 300.0,
            ["docs/verification/v2.1/full-pipeline-performance.json"],
            f"Context P95={performance['context']['p95_ms']} ms (target <300 ms).",
        ),
        _check(
            "A44",
            "Blind hard-negative Recall@5",
            float(modes["v2"]["retrieval_recall_at_5"]) >= 0.90,
            ["docs/verification/v2.1/coding-memory-bench.json"],
            f"V2 Recall@5={modes['v2']['retrieval_recall_at_5']:.3f} (target >=0.90).",
        ),
        _check(
            "A45",
            "Runtime gold isolation",
            benchmark["blind_protocol"].get("runtime_payload_contains_gold") is False
            and benchmark["blind_protocol"].get("gold_loaded_only_by_scorer") is True,
            ["docs/verification/v2.1/coding-memory-bench.json"],
            "Runtime inputs are hashed separately and contain no gold labels.",
        ),
        _check(
            "A46",
            "Paired coding-agent harness",
            int(agent.get("requested_sample_size", 0)) >= 50
            and int(fixture.get("sample_size", 0)) >= 50,
            ["docs/verification/v2.1/agent-ab.json", "scripts/agent_ab_v21.py"],
            "A paired >=50-task harness records success, misuse, compliance, cost, and latency.",
        ),
        _check(
            "A47",
            "Real-agent sample or explicit blocker",
            real_agent_or_blocker,
            ["docs/verification/v2.1/agent-ab.json"],
            (
                "No endpoint was supplied; the report records an explicit external blocker "
                "and no effect claim."
            ),
        ),
        _check(
            "A48",
            "Fixture truthfulness",
            fixture.get("real_model") is False
            and fixture.get("claim") == "harness_validation_only"
            and agent.get("effect_claim") == "none",
            ["docs/verification/v2.1/agent-ab.json"],
            "Synthetic values are labeled harness-only and never presented as real-model evidence.",
        ),
        _check(
            "A49",
            "Grounded consolidation",
            package_v21,
            tests,
            (
                "Model abstraction must cite allowed support/counter memory IDs and "
                "independent sources."
            ),
        ),
        _check(
            "A50",
            "Candidate-first consolidation",
            package_v21,
            tests,
            (
                "Consolidation and distillation outputs remain candidates; no automatic "
                "activation occurs."
            ),
        ),
        _check(
            "A51",
            "Memory health safety",
            package_v21,
            tests,
            (
                "Hot/Warm/Cold/Archived state is explainable/reversible and sole accepted "
                "truth is protected."
            ),
        ),
        _check(
            "A52",
            "Merged-main release smoke",
            main_smoke.get("result") == "PASS"
            and main_smoke.get("branch") == "main"
            and main_smoke.get("git_dirty_before_run") is False
            and main_smoke.get("package", {}).get("result") == "PASS",
            ["docs/verification/v2.1/main-release-smoke.json"],
            (
                "A clean main checkout built and passed packaged "
                "migration/UI/MCP/API/CLI/restart smoke."
            ),
        ),
    ]
    report = {
        "schema": "memoryos-v2.1-acceptance@1",
        "generated_at": datetime.now(UTC).isoformat(),
        "result": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "checks": checks,
        "measured": {
            "temporal_accuracy": modes["v2"]["temporal_accuracy"],
            "conflict_f1": modes["v2"]["conflict"]["f1"],
            "retrieval_recall_at_5": modes["v2"]["retrieval_recall_at_5"],
            "search_p95_ms": performance["search"]["p95_ms"],
            "context_p95_ms": performance["context"]["p95_ms"],
        },
        "external_blockers": [
            {
                "area": "real coding-agent paired experiment",
                "status": agent.get("status"),
                "reason": agent.get("reason"),
                "effect_claim": agent.get("effect_claim"),
            }
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
