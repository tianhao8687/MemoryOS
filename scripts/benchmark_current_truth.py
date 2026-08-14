"""Benchmark Current Truth query scaling against a reproducible SQLite corpus."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import (
    ClaimIdentityRow,
    ClaimRow,
    ClaimVersionRow,
    EntityRow,
    MemoryRow,
)
from memoryos.domain.schemas import (
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimStaleState,
    ClaimStatus,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    MemoryStatus,
    MemoryType,
    ScopeType,
    Sensitivity,
)
from memoryos.engine import MemoryService

DEFAULT_SIZES = (1, 10, 1000)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[position]


def _seed_truth_identities(database: Database, *, scope_key: str, count: int) -> None:
    recorded_at = datetime(2026, 1, 1, tzinfo=UTC)
    entities: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    for index in range(count):
        suffix = f"{count:04d}-{index:04d}"
        entity_id = f"entity-{suffix}"
        memory_id = f"memory-{suffix}"
        identity_id = f"identity-{suffix}"
        claim_id = f"claim-{suffix}"
        entities.append(
            {
                "id": entity_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "entity_type": EntityType.PROJECT,
                "canonical_name": f"Subject {suffix}",
                "normalized_name": f"subject-{suffix}",
                "aliases_json": [],
                "stable_external_key": None,
                "redirect_to_id": None,
                "created_at": recorded_at,
                "updated_at": recorded_at,
            }
        )
        memories.append(
            {
                "id": memory_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "memory_type": MemoryType.PROJECT,
                "category": "decision",
                "subject": None,
                "key": f"truth.{suffix}",
                "title": f"Truth {suffix}",
                "content": f"Subject {suffix} uses value {suffix}.",
                "status": MemoryStatus.ACTIVE,
                "confidence": 0.9,
                "importance": 0.8,
                "valid_from": None,
                "valid_to": None,
                "ttl_seconds": None,
                "supersedes_id": None,
                "created_at": recorded_at,
                "updated_at": recorded_at,
                "created_by": CreatedBy.MANUAL,
                "sensitivity": Sensitivity.NORMAL,
                "metadata_json": {},
            }
        )
        identities.append(
            {
                "id": identity_id,
                "scope_type": ScopeType.REPOSITORY,
                "scope_key": scope_key,
                "subject_entity_id": entity_id,
                "canonical_subject": f"subject-{suffix}",
                "canonical_predicate": "uses",
                "stable_identity": f"{index + count:064x}",
                "created_at": recorded_at,
            }
        )
        claims.append(
            {
                "id": claim_id,
                "memory_id": memory_id,
                "subject_entity_id": entity_id,
                "predicate": "uses",
                "object_kind": ClaimObjectKind.LITERAL,
                "object_entity_id": None,
                "object_value": f"value-{suffix}",
                "polarity": ClaimPolarity.POSITIVE,
                "modality": ClaimModality.DECISION,
                "qualifiers_json": {},
                "canonical_key": f"subject-{suffix}|uses|value-{suffix}|positive",
                "confidence": 0.9,
                "status": ClaimStatus.ACCEPTED,
                "valid_from": None,
                "valid_to": None,
                "recorded_at": recorded_at,
                "stale_state": ClaimStaleState.FRESH,
            }
        )
        versions.append(
            {
                "id": f"version-{suffix}",
                "claim_id": claim_id,
                "identity_id": identity_id,
                "memory_id": memory_id,
                "version_number": 1,
                "object_kind": ClaimObjectKind.LITERAL,
                "object_entity_id": None,
                "object_value": f"value-{suffix}",
                "polarity": ClaimPolarity.POSITIVE,
                "modality": ClaimModality.DECISION,
                "qualifiers_json": {},
                "valid_from": None,
                "valid_to": None,
                "transaction_from": recorded_at,
                "transaction_to": None,
                "status": ClaimStatus.ACCEPTED,
                "stale_state": ClaimStaleState.FRESH,
                "confidence": 0.9,
                "reason": "current-truth performance corpus",
                "actor": "benchmark",
                "source_event_id": None,
                "created_at": recorded_at,
            }
        )
    with database.session() as session:
        session.execute(EntityRow.__table__.insert(), entities)
        session.execute(MemoryRow.__table__.insert(), memories)
        session.execute(ClaimIdentityRow.__table__.insert(), identities)
        session.execute(ClaimRow.__table__.insert(), claims)
        session.execute(ClaimVersionRow.__table__.insert(), versions)


def _invoke(service: MemoryService, scope_key: str) -> tuple[dict[str, Any], int, float]:
    query_count = 0

    def count_query(*_args: Any, **_kwargs: Any) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(service.database.engine, "before_cursor_execute", count_query)
    started = time.perf_counter()
    try:
        result = service.current_truth(
            CurrentTruthRequest(
                scope_type=ScopeType.REPOSITORY,
                scope_key=scope_key,
                predicate="uses",
            )
        )
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        event.remove(service.database.engine, "before_cursor_execute", count_query)
    return result, query_count, elapsed_ms


def _measure(service: MemoryService, *, size: int, rounds: int) -> dict[str, Any]:
    scope_key = f"truth-scale-{size}"
    warmup, _, _ = _invoke(service, scope_key)
    if len(warmup["accepted_claims"]) != size:
        raise RuntimeError(f"warmup returned {len(warmup['accepted_claims'])}, expected {size}")

    query_counts: list[int] = []
    latencies: list[float] = []
    for _ in range(rounds):
        result, query_count, elapsed_ms = _invoke(service, scope_key)
        if result["state"] != "resolved" or len(result["accepted_claims"]) != size:
            raise RuntimeError(f"Current Truth returned an invalid result for size {size}")
        query_counts.append(query_count)
        latencies.append(elapsed_ms)

    return {
        "identities": size,
        "returned_truths": size,
        "rounds": rounds,
        "sql_queries": {
            "min": min(query_counts),
            "max": max(query_counts),
            "median": statistics.median(query_counts),
        },
        "latency_ms": {
            "p50": round(statistics.median(latencies), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "min": round(min(latencies), 3),
            "max": round(max(latencies), 3),
        },
    }


def _comparison(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for size, current in candidate["measurements"].items():
        previous = baseline["measurements"][size]
        previous_queries = int(previous["sql_queries"]["max"])
        current_queries = int(current["sql_queries"]["max"])
        previous_p95 = float(previous["latency_ms"]["p95"])
        current_p95 = float(current["latency_ms"]["p95"])
        result[size] = {
            "sql_query_reduction": previous_queries - current_queries,
            "sql_query_reduction_ratio": round(
                (previous_queries - current_queries) / previous_queries, 6
            ),
            "p95_speedup": round(previous_p95 / current_p95, 3),
        }
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.data_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="memoryos-current-truth-")
        data_dir = Path(temporary.name)
    else:
        data_dir = args.data_dir.resolve()
        if (data_dir / "memoryos.db").exists():
            raise FileExistsError(f"refusing to reuse benchmark database: {data_dir}")

    database = Database(settings_for(data_dir))
    try:
        database.initialize()
        for size in DEFAULT_SIZES:
            _seed_truth_identities(database, scope_key=f"truth-scale-{size}", count=size)
        service = MemoryService(database, database.settings)
        measurements = {
            str(size): _measure(service, size=size, rounds=args.rounds) for size in DEFAULT_SIZES
        }
        report: dict[str, Any] = {
            "schema": "memoryos-current-truth-performance@1",
            "generated_at": datetime.now(UTC).isoformat(),
            "implementation": args.implementation,
            "schema_revision": database.schema_version(),
            "dataset": {
                "kind": "deterministic_synthetic_bitemporal",
                "sizes": list(DEFAULT_SIZES),
                "total_identities": sum(DEFAULT_SIZES),
                "effect_claim": "none",
            },
            "platform": {
                "python": platform.python_version(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "measurements": measurements,
            "gates": {
                "exact_results": True,
                "constant_query_bound": (
                    measurements["1000"]["sql_queries"]["max"]
                    <= measurements["1"]["sql_queries"]["max"] + 12
                ),
            },
        }
        if args.baseline_report is not None:
            baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
            report["baseline"] = baseline
            report["comparison"] = _comparison(baseline, report)
        return report
    finally:
        database.close()
        if temporary is not None:
            temporary.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--implementation", default="working-tree")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/v2.2/current-truth-performance.json"),
    )
    parser.add_argument("--baseline-report", type=Path)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("--rounds must be at least 3")
    return args


def main() -> None:
    args = parse_args()
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["gates"]["constant_query_bound"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
