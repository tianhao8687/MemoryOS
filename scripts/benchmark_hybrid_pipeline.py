"""Tier 2 local hybrid benchmark with real FastEmbed vectors and production stages."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import math
import platform
import statistics
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
from sqlalchemy import func, insert, select

from memoryos.config import settings_for
from memoryos.context import TaskAwareContextCompiler
from memoryos.db.models import (
    ClaimEvidenceRow,
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    EmbeddingRow,
    EntityRow,
    MemoryRow,
    MemorySourceRow,
    SourceRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimRelationType,
    ClaimStaleState,
    ClaimStatus,
    ContextRequest,
    CreatedBy,
    EntityType,
    MemoryStatus,
    MemoryType,
    RelationMethod,
    ScopeType,
    SearchRequest,
    Sensitivity,
    SourceType,
)
from memoryos.evaluation.fastembed_public_training import (
    DEFAULT_FASTEMBED_MODEL,
    _directory_sha256,
    _resolve_model_snapshot,
)
from memoryos.providers.base import ProviderMetadata
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2 import RetrievalPipeline

TECHNOLOGIES = ("redis", "postgresql", "fastapi", "django", "sqlite", "react", "python", "rust")
SCOPE_KEY = "hybrid-performance-repo"


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[position]


class LocalFastEmbedProvider:
    def __init__(
        self,
        *,
        dependency_path: Path,
        model_cache: Path,
        model_name: str,
        threads: int,
        batch_size: int,
    ) -> None:
        resolved_dependencies = dependency_path.resolve(strict=True)
        resolved_cache = model_cache.resolve(strict=True)
        if str(resolved_dependencies) not in sys.path:
            sys.path.append(str(resolved_dependencies))
        fastembed: Any = importlib.import_module("fastembed")
        descriptions = {
            str(item["model"]): item for item in fastembed.TextEmbedding.list_supported_models()
        }
        description = descriptions.get(model_name)
        if description is None:
            raise ValueError(f"FastEmbed does not support model {model_name}")
        sources = cast(dict[str, object], description["sources"])
        hugging_face_source = sources.get("hf")
        if not isinstance(hugging_face_source, str) or not hugging_face_source:
            raise ValueError("FastEmbed model is missing a hashable Hugging Face source")
        self._model = fastembed.TextEmbedding(
            model_name=model_name,
            cache_dir=str(resolved_cache),
            threads=threads,
        )
        revision, snapshot = _resolve_model_snapshot(resolved_cache, hugging_face_source)
        self._name = "fastembed-local"
        self._model_name = model_name
        self._batch_size = batch_size
        self.identity = {
            "provider": self._name,
            "model": model_name,
            "revision": revision,
            "dimensions": int(description["dim"]),
            "fastembed_version": importlib.metadata.version("fastembed"),
            "model_files_sha256": _directory_sha256(snapshot),
            "fixture": False,
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider=self.name,
            model=self.model,
            real_model=True,
            max_input_chars=12_000,
            capabilities=("embedding", "query_instruction", "document_instruction"),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        rows = list(self._model.query_embed([text[:12_000]], batch_size=1))
        return [float(value) for value in rows[0]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        rows = list(
            self._model.passage_embed(
                [text[:12_000] for text in texts],
                batch_size=self._batch_size,
            )
        )
        return [[float(value) for value in row] for row in rows]


def _stable_id(kind: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoryos-tier2-{kind}-{index}"))


def _seed(database: Database, records: int) -> tuple[list[str], float]:
    started = time.perf_counter()
    now = datetime.now(UTC)
    split = now - timedelta(days=10)
    entity_ids = {
        technology: str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoryos-tier2-entity-{technology}"))
        for technology in TECHNOLOGIES
    }
    with database.session() as session:
        session.execute(
            insert(EntityRow),
            [
                {
                    "id": entity_id,
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": SCOPE_KEY,
                    "entity_type": EntityType.DEPENDENCY,
                    "canonical_name": technology,
                    "normalized_name": technology,
                    "aliases_json": [],
                    "stable_external_key": None,
                    "redirect_to_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for technology, entity_id in entity_ids.items()
            ],
        )
    memory_ids: list[str] = []
    for start in range(0, records, 500):
        memories: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        claims: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for index in range(start, min(start + 500, records)):
            technology = TECHNOLOGIES[index % len(TECHNOLOGIES)]
            historical = index % 4 == 0
            valid_from = now - timedelta(days=30) if historical else split
            valid_to = split if historical else None
            memory_id = _stable_id("memory", index)
            source_id = _stable_id("source", index)
            claim_id = _stable_id("claim", index)
            content = (
                f"The current production {technology} adapter handles authentication session "
                f"rotation for component {index % 997}."
            )
            evidence_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            memory_ids.append(memory_id)
            memories.append(
                {
                    "id": memory_id,
                    "scope_type": ScopeType.REPOSITORY,
                    "scope_key": SCOPE_KEY,
                    "memory_type": MemoryType.PROJECT,
                    "category": "decision",
                    "subject": technology,
                    "key": f"tier2.{technology}.{index}",
                    "title": f"{technology.title()} adapter decision {index}",
                    "content": content,
                    "status": MemoryStatus.ACTIVE,
                    "confidence": 0.9,
                    "importance": 0.75,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "ttl_seconds": None,
                    "supersedes_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": CreatedBy.IMPORT,
                    "sensitivity": Sensitivity.NORMAL,
                    "metadata_json": {
                        "fixture": "tier2-hybrid-performance",
                        "index": index,
                    },
                }
            )
            sources.append(
                {
                    "id": source_id,
                    "source_type": SourceType.IMPORT,
                    "source_ref": f"tier2-benchmark:{index}",
                    "captured_at": now,
                    "excerpt": content,
                    "content_hash": evidence_hash,
                    "metadata_json": {"fixture": "tier2-hybrid-performance"},
                    "created_at": now,
                }
            )
            links.append({"memory_id": memory_id, "source_id": source_id})
            claims.append(
                {
                    "id": claim_id,
                    "memory_id": memory_id,
                    "subject_entity_id": entity_ids[technology],
                    "predicate": "uses",
                    "object_kind": ClaimObjectKind.LITERAL,
                    "object_entity_id": None,
                    "object_value": f"adapter-{index % 997}",
                    "polarity": ClaimPolarity.POSITIVE,
                    "modality": ClaimModality.DECISION,
                    "qualifiers_json": {},
                    "canonical_key": f"{technology}|uses|adapter-{index % 997}|positive",
                    "confidence": 0.9,
                    "status": ClaimStatus.ACCEPTED,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "recorded_at": now,
                    "stale_state": ClaimStaleState.FRESH,
                }
            )
            evidence_rows.append(
                {
                    "id": _stable_id("evidence", index),
                    "claim_id": claim_id,
                    "source_id": source_id,
                    "evidence_excerpt": content,
                    "evidence_hash": evidence_hash,
                    "source_anchor_id": None,
                    "support_weight": 1.0,
                }
            )
            if index % 100 == 1 and index > 0:
                relations.append(
                    {
                        "id": _stable_id("relation", index),
                        "from_claim_id": _stable_id("claim", index),
                        "to_claim_id": _stable_id("claim", index - 1),
                        "relation_type": ClaimRelationType.DEPENDS_ON,
                        "confidence": 1.0,
                        "method": RelationMethod.RULE,
                        "explanation": "Synthetic one-hop performance relation",
                        "created_at": now,
                    }
                )
        with database.session() as session:
            session.execute(insert(SourceRow), sources)
            session.execute(insert(MemoryRow), memories)
            session.execute(insert(MemorySourceRow), links)
            session.execute(insert(ClaimRow), claims)
            session.execute(insert(ClaimEvidenceRow), evidence_rows)
            if relations:
                session.execute(insert(ClaimRelationRow), relations)
    database.checkpoint()
    return memory_ids, time.perf_counter() - started


def _embed(
    database: Database,
    provider: LocalFastEmbedProvider,
    memory_ids: list[str],
    *,
    batch_size: int,
) -> float:
    started = time.perf_counter()
    for start in range(0, len(memory_ids), batch_size):
        selected_ids = memory_ids[start : start + batch_size]
        with database.session() as session:
            rows = list(
                session.scalars(
                    select(MemoryRow).where(MemoryRow.id.in_(selected_ids)).order_by(MemoryRow.id)
                )
            )
        documents = [f"{row.title}\n{row.content}" for row in rows]
        vectors = provider.embed_documents(documents)
        if len(vectors) != len(rows):
            raise RuntimeError("real embedding provider returned an incomplete batch")
        embedded_at = datetime.now(UTC)
        with database.session() as session:
            session.execute(
                insert(EmbeddingRow),
                [
                    {
                        "id": _stable_id("embedding", start + offset),
                        "memory_id": row.id,
                        "provider": provider.name,
                        "model": provider.model,
                        "dimensions": len(vector),
                        "vector_blob": np.asarray(vector, dtype=np.float32).tobytes(),
                        "vector_json": vector,
                        "created_at": embedded_at,
                    }
                    for offset, (row, vector) in enumerate(zip(rows, vectors, strict=True))
                ],
            )
    database.checkpoint()
    return time.perf_counter() - started


def _measure_mode(
    database: Database,
    provider: LocalFastEmbedProvider,
    *,
    rounds: int,
    ann_enabled: bool,
) -> dict[str, Any]:
    database.settings.ann_enabled = ann_enabled
    engine = RetrievalEngine(database, provider)
    rebuild = engine.rebuild_ann_index()
    pipeline = RetrievalPipeline(database, engine)
    compiler = TaskAwareContextCompiler(pipeline)
    now = datetime.now(UTC)
    scenarios = (
        (
            "hybrid",
            SearchRequest(
                query="current production fastapi authentication session",
                scope_type=ScopeType.REPOSITORY,
                scope_key=SCOPE_KEY,
                limit=10,
            ),
        ),
        (
            "claim_relation",
            SearchRequest(
                query="why postgresql",
                scope_type=ScopeType.REPOSITORY,
                scope_key=SCOPE_KEY,
                limit=10,
            ),
        ),
        (
            "temporal",
            SearchRequest(
                query="historical redis",
                scope_type=ScopeType.REPOSITORY,
                scope_key=SCOPE_KEY,
                as_of_valid_time=now - timedelta(days=20),
                as_known_at=now + timedelta(minutes=1),
                limit=10,
            ),
        ),
    )
    scenario_ms: dict[str, list[float]] = defaultdict(list)
    context_ms: list[float] = []
    requested_channels: set[str] = set()
    executed_channels: set[str] = set()
    contributing_channels: set[str] = set()
    degraded_channels: set[str] = set()
    pipeline_modes: set[str] = set()
    reranker_modes: set[str] = set()
    for iteration in range(rounds + 2):
        for scenario, request in scenarios:
            started = time.perf_counter()
            result = pipeline.search(request)
            elapsed = (time.perf_counter() - started) * 1000.0
            if iteration >= 2:
                scenario_ms[scenario].append(elapsed)
            if not result["items"]:
                raise RuntimeError(f"Tier 2 {scenario} query returned no candidates")
            routing = result["query_plan"]["routing"]
            requested_channels.update(str(item) for item in routing["requested_channels"])
            executed_channels.update(str(item) for item in routing["executed_channels"])
            contributing_channels.update(str(item) for item in routing["contributing_channels"])
            degraded_channels.update(str(item) for item in routing["degraded_channels"])
            pipeline_modes.add(str(result["pipeline_mode"]))
            reranker_modes.add(str(result["reranker"]))
        started = time.perf_counter()
        context = compiler.build(
            ContextRequest(
                task="implement current production fastapi authentication session",
                repository=SCOPE_KEY,
                budget=6000,
            )
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        if iteration >= 2:
            context_ms.append(elapsed)
        if not context["manifest"]:
            raise RuntimeError("Tier 2 context compiler returned no manifest")
    all_search_ms = [value for values in scenario_ms.values() for value in values]
    status = engine.vector_status()
    engine.close()
    return {
        "ann_enabled": ann_enabled,
        "ann_rebuild": rebuild,
        "vector_status": status,
        "pipeline_modes": sorted(pipeline_modes),
        "channels": {
            "requested": sorted(requested_channels),
            "executed": sorted(executed_channels),
            "contributing": sorted(contributing_channels),
            "degraded": sorted(degraded_channels),
        },
        "reranker": sorted(reranker_modes),
        "search": {
            "p50_ms": round(statistics.median(all_search_ms), 3),
            "p95_ms": round(_percentile(all_search_ms, 0.95), 3),
            "scenarios": {
                key: {
                    "p50_ms": round(statistics.median(values), 3),
                    "p95_ms": round(_percentile(values, 0.95), 3),
                }
                for key, values in sorted(scenario_ms.items())
            },
        },
        "context": {
            "p50_ms": round(statistics.median(context_ms), 3),
            "p95_ms": round(_percentile(context_ms, 0.95), 3),
        },
    }


def run(
    *,
    records: int,
    rounds: int,
    data_dir: Path,
    dependency_path: Path,
    model_cache: Path,
    model_name: str,
    threads: int,
    batch_size: int,
) -> dict[str, Any]:
    if records not in {10_000, 20_000}:
        raise ValueError("Tier 2 requires exactly 10,000 or 20,000 records")
    if rounds < 3:
        raise ValueError("Tier 2 requires at least three measured rounds")
    settings = settings_for(data_dir)
    if settings.database_path.exists():
        raise ValueError("Tier 2 requires a fresh data directory")
    database = Database(settings)
    database.initialize()
    provider = LocalFastEmbedProvider(
        dependency_path=dependency_path,
        model_cache=model_cache,
        model_name=model_name,
        threads=threads,
        batch_size=batch_size,
    )
    try:
        memory_ids, seed_seconds = _seed(database, records)
        embedding_seconds = _embed(database, provider, memory_ids, batch_size=batch_size)
        ann = _measure_mode(database, provider, rounds=rounds, ann_enabled=True)
        exact = _measure_mode(database, provider, rounds=rounds, ann_enabled=False)
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
        sqlite_vec_version = importlib.metadata.version("sqlite-vec")
        gates = {
            "record_count": counts["memories"] == records,
            "real_embeddings": counts["embeddings"] == records and provider.metadata.real_model,
            "claim_relation_data": counts["claims"] == records and counts["relations"] > 0,
            "ann_executed": any(
                item.get("status") == "ready" and int(item.get("count", 0)) == records
                for item in ann["ann_rebuild"].get("namespaces", [])
            ),
            "exact_fallback_executed": any(
                "exact-fallback" in mode for mode in exact["pipeline_modes"]
            ),
            "vector_channel": "vector" in ann["channels"]["contributing"],
            "claim_relation_channel": "graph" in ann["channels"]["contributing"],
            "temporal_channel": "temporal" in ann["channels"]["contributing"],
        }
        return {
            "schema": "memoryos-performance-tier-report@1",
            "tier": "tier_2_10k_20k_hybrid_local",
            "label": f"{records // 1000}K Hybrid Local Pipeline",
            "generated_at": datetime.now(UTC).isoformat(),
            "evidence_type": "real_local_embedding_performance_fixture",
            "effect_claim": "none",
            "manual_or_scheduled_only": True,
            "ci_claim": False,
            "records": records,
            "rounds": rounds,
            "seed_seconds": round(seed_seconds, 3),
            "embedding_seconds": round(embedding_seconds, 3),
            "record_counts": counts,
            "provider": provider.identity,
            "vector_backend": {
                "primary": "sqlite-vec",
                "sqlite_vec_version": sqlite_vec_version,
                "fallback": "exact-numpy",
            },
            "reranker": {"configured": False, "mode": "disabled"},
            "fixture": "synthetic memories/claims/relations with real local BGE embeddings",
            "modes": {"sqlite_vec_ann": ann, "exact_fallback": exact},
            "platform": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "gates": gates,
            "passed": all(gates.values()),
            "limitations": [
                "Synthetic corpus measures local pipeline cost, not coding-agent effectiveness.",
                "ClaimVersion history count is zero; temporal coverage uses Claim valid intervals.",
                "No cross-encoder reranker or relationship model is active in Tier 2.",
                "Tier 2 is manual/scheduled evidence and is not claimed as a default CI job.",
            ],
        }
    finally:
        database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, choices=(10_000, 20_000), default=10_000)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--fastembed-path", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_FASTEMBED_MODEL)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/verification/v2.2/hybrid-local-performance.json"),
    )
    arguments = parser.parse_args()
    if arguments.data_dir is None:
        with tempfile.TemporaryDirectory(prefix="memoryos-tier2-") as directory:
            report = run(
                records=arguments.records,
                rounds=arguments.rounds,
                data_dir=Path(directory),
                dependency_path=arguments.fastembed_path,
                model_cache=arguments.model_cache,
                model_name=arguments.model,
                threads=arguments.threads,
                batch_size=arguments.batch_size,
            )
    else:
        report = run(
            records=arguments.records,
            rounds=arguments.rounds,
            data_dir=arguments.data_dir,
            dependency_path=arguments.fastembed_path,
            model_cache=arguments.model_cache,
            model_name=arguments.model,
            threads=arguments.threads,
            batch_size=arguments.batch_size,
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
