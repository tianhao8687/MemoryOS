"""Measure FTS-only search and context latency against 10,000 local records."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import insert

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import MemoryRow, MemorySourceRow, SourceRow
from memoryos.domain.schemas import (
    ContextRequest,
    CreatedBy,
    MemoryStatus,
    MemoryType,
    ScopeType,
    Sensitivity,
    SourceType,
)
from memoryos.engine import MemoryService

ROOT = Path(__file__).resolve().parents[1]


def _seed(database: Database, records: int) -> float:
    started = time.perf_counter()
    now = datetime.now(UTC)
    batch_size = 1_000
    for start in range(0, records, batch_size):
        memories: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        for index in range(start, min(start + batch_size, records)):
            memory_id = str(uuid.uuid4())
            source_id = str(uuid.uuid4())
            relevant = index % 20 == 0
            topic = "authentication session" if relevant else f"local component {index % 211}"
            title = f"{topic.title()} decision {index:05d}"
            content = f"Use verified {topic} behavior for benchmark record {index:05d}."
            memories.append(
                {
                    "id": memory_id,
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": "benchmark-repo",
                    "memory_type": MemoryType.PROJECT,
                    "category": "decision",
                    "subject": topic,
                    "key": f"benchmark.{index:05d}",
                    "title": title,
                    "content": content,
                    "status": MemoryStatus.ACTIVE,
                    "confidence": 0.85,
                    "importance": 0.7,
                    "valid_from": None,
                    "valid_to": None,
                    "ttl_seconds": None,
                    "supersedes_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": CreatedBy.IMPORT,
                    "sensitivity": Sensitivity.NORMAL,
                    "metadata_json": {"fixture": "performance", "index": index},
                }
            )
            sources.append(
                {
                    "id": source_id,
                    "source_type": SourceType.IMPORT,
                    "source_ref": f"benchmark:{index:05d}",
                    "captured_at": now,
                    "excerpt": content,
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                    "metadata_json": {"fixture": "performance"},
                    "created_at": now,
                }
            )
            links.append({"memory_id": memory_id, "source_id": source_id})
        with database.session() as session:
            session.execute(insert(SourceRow), sources)
            session.execute(insert(MemoryRow), memories)
            session.execute(insert(MemorySourceRow), links)
    database.checkpoint()
    return time.perf_counter() - started


def _measure(service: MemoryService, rounds: int) -> tuple[list[float], list[float]]:
    from memoryos.domain.schemas import SearchRequest

    search_request = SearchRequest(query="authentication session", limit=50)
    context_request = ContextRequest(
        task="implement authentication session rotation",
        repository="benchmark-repo",
        branch="main",
        budget=6_000,
    )
    assert service.search(search_request)["total"] > 0
    assert "Authentication" in service.context(context_request)["text"]
    search_times: list[float] = []
    context_times: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        service.search(search_request)
        search_times.append(time.perf_counter() - started)
        started = time.perf_counter()
        service.context(context_request)
        context_times.append(time.perf_counter() - started)
    return search_times, context_times


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs" / "verification" / "performance.json"
    )
    args = parser.parse_args()
    if args.records < 1_000 or args.rounds < 3:
        raise SystemExit("benchmark requires at least 1,000 records and 3 rounds")
    with tempfile.TemporaryDirectory(prefix="memoryos-performance-") as directory:
        settings = settings_for(Path(directory))
        database = Database(settings)
        database.initialize()
        try:
            seed_seconds = _seed(database, args.records)
            search_times, context_times = _measure(MemoryService(database, settings), args.rounds)
        finally:
            database.close()
    report = {
        "result": "PASS",
        "records": args.records,
        "rounds": args.rounds,
        "mode": "fts5",
        "seed_seconds": round(seed_seconds, 6),
        "search_ms": {
            "median": round(statistics.median(search_times) * 1_000, 3),
            "max": round(max(search_times) * 1_000, 3),
        },
        "context_ms": {
            "median": round(statistics.median(context_times) * 1_000, 3),
            "max": round(max(context_times) * 1_000, 3),
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    if max(search_times) >= 1.0 or max(context_times) >= 1.0:
        report["result"] = "FAIL"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["result"] != "PASS":
        raise SystemExit("FTS-only search/context exceeded the one-second acceptance threshold")


if __name__ == "__main__":
    main()
