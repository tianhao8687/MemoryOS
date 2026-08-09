from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memoryos.config import settings_for
from memoryos.context import TaskAwareContextCompiler
from memoryos.db.models import MemoryRow
from memoryos.db.session import Database
from memoryos.domain.schemas import ContextRequest, SearchRequest
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2 import RetrievalPipeline


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, int((len(ordered) - 1) * quantile))
    return ordered[position]


def _insert_records(database: Database, records: int) -> None:
    now = datetime.now(UTC)
    with database.engine.begin() as connection:
        for start in range(0, records, 5000):
            batch: list[dict[str, Any]] = []
            for index in range(start, min(start + 5000, records)):
                token = f"adapter{index % 1000}"
                batch.append(
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoryos-bench-{index}")),
                        "scope_type": "repository",
                        "scope_key": "bench-repo",
                        "memory_type": "project",
                        "category": "decision",
                        "subject": "benchmark",
                        "key": f"benchmark.{index}",
                        "title": f"Decision {token} record {index}",
                        "content": (
                            f"The current production component uses {token}. "
                            f"Bounded benchmark evidence record {index}."
                        ),
                        "status": "active",
                        "confidence": 0.9,
                        "importance": 0.7,
                        "valid_from": None,
                        "valid_to": None,
                        "ttl_seconds": None,
                        "supersedes_id": None,
                        "created_at": now,
                        "updated_at": now,
                        "created_by": "import",
                        "sensitivity": "normal",
                        "metadata_json": {"fixture": "v2.1-full-pipeline"},
                    }
                )
            connection.execute(MemoryRow.__table__.insert(), batch)


def run(records: int, rounds: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="memoryos-v21-bench-") as directory:
        settings = settings_for(Path(directory))
        database = Database(settings)
        database.initialize()
        insert_started = time.perf_counter()
        _insert_records(database, records)
        insert_seconds = time.perf_counter() - insert_started
        engine = RetrievalEngine(database)
        pipeline = RetrievalPipeline(database, engine)
        compiler = TaskAwareContextCompiler(pipeline)
        search_ms: list[float] = []
        context_ms: list[float] = []
        for index in range(rounds + 3):
            token = f"adapter{(index * 37) % 1000}"
            started = time.perf_counter()
            search = pipeline.search(
                SearchRequest(
                    query=f"current production {token}",
                    scope_key="bench-repo",
                    limit=10,
                )
            )
            elapsed = (time.perf_counter() - started) * 1000
            if index >= 3:
                search_ms.append(elapsed)
            if not search["items"]:
                raise RuntimeError("full-pipeline benchmark search returned no candidates")
            started = time.perf_counter()
            context = compiler.build(
                ContextRequest(
                    task=f"implement current production {token}",
                    repository="bench-repo",
                    budget=3000,
                )
            )
            elapsed = (time.perf_counter() - started) * 1000
            if index >= 3:
                context_ms.append(elapsed)
            if not context["manifest"]:
                raise RuntimeError("full-pipeline context compilation returned no manifest")
        report = {
            "schema": "memoryos-v2.1-full-pipeline-performance@1",
            "generated_at": datetime.now(UTC).isoformat(),
            "records": records,
            "rounds": rounds,
            "insert_seconds": round(insert_seconds, 3),
            "search": {
                "p50_ms": round(statistics.median(search_ms), 3),
                "p95_ms": round(_percentile(search_ms, 0.95), 3),
                "target_p95_ms": 150.0,
            },
            "context": {
                "p50_ms": round(statistics.median(context_ms), 3),
                "p95_ms": round(_percentile(context_ms, 0.95), 3),
                "target_p95_ms": 300.0,
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "pipeline": "RetrievalPipeline + TaskAwareContextCompiler",
            },
        }
        report["passed"] = bool(
            records >= 100_000
            and report["search"]["p95_ms"] < 150.0
            and report["context"]["p95_ms"] < 300.0
        )
        engine.close()
        database.close()
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark V2.1 full retrieval/context pipeline")
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--rounds", type=int, default=25)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/v2.1/full-pipeline-performance.json"),
    )
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    report = run(args.records, args.rounds)
    if args.development and args.records < 100_000:
        report["passed"] = None
        report["development_only"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
