"""Run a separate Tier 3 benchmark only when real model providers are configured."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select

from memoryos.config import settings_for
from memoryos.context import TaskAwareContextCompiler
from memoryos.db import Database
from memoryos.db.models import (
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    EmbeddingRow,
    MemoryRow,
)
from memoryos.domain.schemas import ContextRequest, ScopeType, SearchRequest
from memoryos.engine import MemoryService
from memoryos.evaluation import ProductionCodingMemoryBench


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[position]


def _endpoint_origin(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or "unknown"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def run(
    *,
    data_dir: Path,
    rounds: int,
    embedding_base_url: str,
    embedding_model: str,
    extractor_base_url: str,
    reranker_model: str,
    relationship_model: str,
    embedding_api_key: str | None,
    extractor_api_key: str | None,
) -> dict[str, Any]:
    if rounds < 3:
        raise ValueError("Tier 3 requires at least three measured rounds")
    settings = settings_for(
        data_dir,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_api_key=embedding_api_key,
        extractor_base_url=extractor_base_url,
        reranker_model=reranker_model,
        relationship_model=relationship_model,
        extractor_api_key=extractor_api_key,
    )
    if settings.database_path.exists():
        raise ValueError("Tier 3 requires a fresh data directory")
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    try:
        runtime, _gold = ProductionCodingMemoryBench()._seed(service)
        compiler = TaskAwareContextCompiler(service.retrieval_v2)
        search_ms: list[float] = []
        context_ms: list[float] = []
        reranker_modes: set[str] = set()
        requested_channels: set[str] = set()
        executed_channels: set[str] = set()
        contributing_channels: set[str] = set()
        degraded_channels: set[str] = set()
        for iteration in range(rounds + 2):
            for case in runtime["retrieval"]:
                started = time.perf_counter()
                result = service.search(
                    SearchRequest(
                        query=str(case["query"]),
                        scope_type=ScopeType(str(case["scope_type"])),
                        scope_key=str(case["scope_key"]),
                        limit=5,
                    )
                )
                elapsed = (time.perf_counter() - started) * 1000.0
                if iteration >= 2:
                    search_ms.append(elapsed)
                if not result["items"]:
                    raise RuntimeError("Tier 3 search returned no candidates")
                reranker_modes.add(str(result["reranker"]))
                routing = result["query_plan"]["routing"]
                requested_channels.update(str(item) for item in routing["requested_channels"])
                executed_channels.update(str(item) for item in routing["executed_channels"])
                contributing_channels.update(str(item) for item in routing["contributing_channels"])
                degraded_channels.update(str(item) for item in routing["degraded_channels"])
            first = runtime["retrieval"][0]
            started = time.perf_counter()
            context = compiler.build(
                ContextRequest(
                    task=str(first["query"]),
                    repository=str(first["scope_key"]),
                    budget=3000,
                )
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            if iteration >= 2:
                context_ms.append(elapsed)
            if not context["manifest"]:
                raise RuntimeError("Tier 3 context returned no manifest")
        model_reranker_executed = bool(reranker_modes) and all(
            mode.startswith("openai-compatible:") for mode in reranker_modes
        )
        gates = {
            "vector_channel": "vector" in contributing_channels,
            "model_reranker_executed": model_reranker_executed,
            "no_provider_fallback": not degraded_channels
            and "provider-fallback" not in reranker_modes,
        }
        with database.session() as session:
            counts = {
                "memories": int(session.scalar(select(func.count()).select_from(MemoryRow)) or 0),
                "embeddings": int(
                    session.scalar(select(func.count()).select_from(EmbeddingRow)) or 0
                ),
                "claims": int(session.scalar(select(func.count()).select_from(ClaimRow)) or 0),
                "claim_versions": int(
                    session.scalar(select(func.count()).select_from(ClaimVersionRow)) or 0
                ),
                "relations": int(
                    session.scalar(select(func.count()).select_from(ClaimRelationRow)) or 0
                ),
            }
        vector_status = service.vector_status()
        return {
            "schema": "memoryos-performance-tier-report@1",
            "tier": "tier_3_model_enhanced",
            "label": "Model-enhanced Pipeline",
            "generated_at": datetime.now(UTC).isoformat(),
            "evidence_type": "configured_model_provider_integration",
            "effect_claim": "none",
            "records": counts["memories"],
            "record_counts": counts,
            "rounds": rounds,
            "fixture": "synthetic production-path integration corpus",
            "providers": {
                "embedding": {
                    "endpoint_origin": _endpoint_origin(embedding_base_url),
                    "model": embedding_model,
                    "timeout_seconds": settings.provider_timeout_seconds,
                    "api_key_recorded": False,
                },
                "reranker": {
                    "endpoint_origin": _endpoint_origin(extractor_base_url),
                    "model": reranker_model,
                    "executed_modes": sorted(reranker_modes),
                    "timeout_seconds": settings.provider_timeout_seconds,
                    "api_key_recorded": False,
                },
                "relationship": {
                    "endpoint_origin": _endpoint_origin(extractor_base_url),
                    "model": relationship_model,
                    "configured": True,
                    "exercised_by_this_retrieval_suite": False,
                    "timeout_seconds": settings.provider_timeout_seconds,
                    "api_key_recorded": False,
                },
            },
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "status": "unavailable_in_openai_compatible_provider_contract",
            },
            "cost": {
                "amount": None,
                "currency": None,
                "status": "unavailable_in_openai_compatible_provider_contract",
            },
            "vector_backend": vector_status,
            "reranker": {
                "configured": True,
                "modes": sorted(reranker_modes),
            },
            "channels": {
                "requested": sorted(requested_channels),
                "executed": sorted(executed_channels),
                "contributing": sorted(contributing_channels),
                "degraded": sorted(degraded_channels),
            },
            "search": {
                "p50_ms": round(statistics.median(search_ms), 3),
                "p95_ms": round(_percentile(search_ms, 0.95), 3),
            },
            "context": {
                "p50_ms": round(statistics.median(context_ms), 3),
                "p95_ms": round(_percentile(context_ms, 0.95), 3),
            },
            "platform": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "fallback_state": "none" if not degraded_channels else sorted(degraded_channels),
            "gates": gates,
            "passed": all(gates.values()),
            "limitations": [
                "This integration benchmark does not estimate coding-agent effectiveness.",
                "The relationship judge is configuration-checked but not invoked by clear cases.",
            ],
        }
    finally:
        service.close()
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--embedding-base-url", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--extractor-base-url", required=True)
    parser.add_argument("--reranker-model", required=True)
    parser.add_argument("--relationship-model", required=True)
    parser.add_argument("--embedding-api-key")
    parser.add_argument("--extractor-api-key")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/v2.2/model-enhanced-performance.json"),
    )
    arguments = parser.parse_args()
    report = run(
        data_dir=arguments.data_dir,
        rounds=arguments.rounds,
        embedding_base_url=arguments.embedding_base_url,
        embedding_model=arguments.embedding_model,
        extractor_base_url=arguments.extractor_base_url,
        reranker_model=arguments.reranker_model,
        relationship_model=arguments.relationship_model,
        embedding_api_key=arguments.embedding_api_key,
        extractor_api_key=arguments.extractor_api_key,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
