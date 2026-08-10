from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter
from sqlalchemy import select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import MemoryRow
from memoryos.domain.schemas import ContextRequest, ScopeType, SearchRequest
from memoryos.engine import MemoryService
from memoryos.errors import MemoryOSError
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    MemorySeedSpec,
)

TOKEN_RE = re.compile(r"[\w.]+", re.UNICODE)


class ReadOnlyMemoryBackend(Protocol):
    name: str

    def context(
        self,
        *,
        task: str,
        branch: str | None,
        workspace: str | None,
        task_scope: str | None,
        budget: int,
    ) -> dict[str, Any]: ...

    def search(self, *, query: str, limit: int) -> dict[str, Any]: ...


class ToolAuditor:
    def __init__(self, path: Path, *, backend: str) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backend = backend

    def record(
        self,
        *,
        tool: str,
        query: str,
        selected_seed_ids: list[str],
        retrieval_run_id: str | None,
        ok: bool,
    ) -> None:
        payload = {
            "schema_version": "1",
            "at": datetime.now(UTC).isoformat(),
            "backend": self.backend,
            "tool": tool,
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "selected_seed_ids": selected_seed_ids,
            "retrieval_run_id": retrieval_run_id,
            "ok": ok,
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


class FlatMemoryBackend:
    name = ExperimentCondition.FLAT_MEMORY.value

    def __init__(self, seeds: list[MemorySeedSpec], cutoff: datetime) -> None:
        self.seeds = seeds
        self.cutoff = cutoff

    def context(
        self,
        *,
        task: str,
        branch: str | None,
        workspace: str | None,
        task_scope: str | None,
        budget: int,
    ) -> dict[str, Any]:
        del branch, workspace, task_scope
        ranked = self._rank(task)
        selected: list[MemorySeedSpec] = []
        lines = ["Flat Memory Context"]
        used = len(lines[0])
        for seed in ranked:
            line = f"- [{seed.id}] {seed.title}: {seed.content} (source: {seed.source_ref})"
            if used + len(line) + 1 > budget:
                continue
            selected.append(seed)
            lines.append(line)
            used += len(line) + 1
        return {
            "task": task,
            "budget": budget,
            "characters_used": used,
            "retrieval_mode": "flat-lexical",
            "retrieval_run_id": None,
            "manifest": [
                {
                    "memory_id": seed.id,
                    "included": seed in selected,
                    "inclusion_reason": "flat lexical order" if seed in selected else None,
                    "exclusion_reason": None if seed in selected else "budget",
                }
                for seed in ranked
            ],
            "text": "\n".join(lines),
            "snapshot_cutoff": self.cutoff.isoformat(),
        }

    def search(self, *, query: str, limit: int) -> dict[str, Any]:
        selected = self._rank(query)[:limit]
        return {
            "items": [
                {
                    "memory": seed.model_dump(mode="json", exclude_none=True),
                    "score": _lexical_score(query, seed),
                }
                for seed in selected
            ],
            "total": len(self.seeds),
            "mode": "flat-lexical",
            "retrieval_run_id": None,
            "snapshot_cutoff": self.cutoff.isoformat(),
        }

    def _rank(self, query: str) -> list[MemorySeedSpec]:
        return sorted(
            self.seeds,
            key=lambda seed: (_lexical_score(query, seed), seed.importance, seed.id),
            reverse=True,
        )


class MemoryOSBackend:
    name = ExperimentCondition.MEMORYOS.value

    def __init__(self, data_dir: Path, repository: str, cutoff: datetime) -> None:
        self.database = Database(settings_for(data_dir))
        self.database.initialize()
        self.service = MemoryService(self.database, self.database.settings)
        self.repository = repository
        self.cutoff = cutoff
        with self.database.session() as session:
            rows = list(session.scalars(select(MemoryRow)))
            self.seed_ids = {
                row.id: str(row.metadata_json["benchmark_seed_id"])
                for row in rows
                if "benchmark_seed_id" in row.metadata_json
            }

    def context(
        self,
        *,
        task: str,
        branch: str | None,
        workspace: str | None,
        task_scope: str | None,
        budget: int,
    ) -> dict[str, Any]:
        return self.service.context(
            ContextRequest(
                task=task,
                repository=self.repository,
                branch=branch,
                workspace=workspace,
                task_scope=task_scope,
                budget=budget,
                as_of_valid_time=self.cutoff,
                as_known_at=self.cutoff,
            )
        )

    def search(self, *, query: str, limit: int) -> dict[str, Any]:
        return self.service.search(
            SearchRequest(
                query=query,
                scope_type=ScopeType.REPOSITORY,
                scope_key=self.repository,
                as_of_valid_time=self.cutoff,
                as_known_at=self.cutoff,
                limit=limit,
            )
        )

    def manifest_seed_ids(self, result: dict[str, Any]) -> list[str]:
        memory_ids: list[str] = []
        manifest = result.get("manifest")
        if isinstance(manifest, list):
            memory_ids.extend(
                str(item.get("memory_id"))
                for item in manifest
                if isinstance(item, dict) and item.get("included") is True
            )
        items = result.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("memory"), dict):
                    continue
                memory_ids.append(str(item["memory"].get("id", "")))
        return sorted({self.seed_ids[item] for item in memory_ids if item in self.seed_ids})


def create_benchmark_mcp(
    backend: ReadOnlyMemoryBackend,
    *,
    repository: str,
    auditor: ToolAuditor,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    mcp = FastMCP(
        "MemoryOS Real Workload Benchmark",
        instructions=(
            "Read-only benchmark memory. Call memory_context before editing the repository."
        ),
        host=host,
        port=port,
        stateless_http=True,
    )

    def call(tool: str, query: str, operation: Any) -> dict[str, Any]:
        try:
            value = operation()
            selected = _selected_seed_ids(backend, value)
            retrieval_run_id = value.get("retrieval_run_id")
            auditor.record(
                tool=tool,
                query=query,
                selected_seed_ids=selected,
                retrieval_run_id=str(retrieval_run_id) if retrieval_run_id else None,
                ok=True,
            )
            return {"ok": True, "result": value}
        except (MemoryOSError, ValueError) as exc:
            auditor.record(
                tool=tool,
                query=query,
                selected_seed_ids=[],
                retrieval_run_id=None,
                ok=False,
            )
            error = (
                exc.as_dict()
                if isinstance(exc, MemoryOSError)
                else {
                    "code": "VALIDATION_ERROR",
                    "message": str(exc),
                    "details": {},
                }
            )
            return {"ok": False, "error": error}

    def require_repository(value: str) -> None:
        if value != repository:
            raise ValueError("benchmark repository scope mismatch")

    @mcp.tool(name="memory_context")
    def memory_context(
        task: str,
        repo: str,
        branch: str | None = None,
        workspace: str | None = None,
        task_scope: str | None = None,
        budget: int = 6000,
    ) -> dict[str, Any]:
        """Retrieve read-only task context from the assigned benchmark memory condition."""

        def operation() -> dict[str, Any]:
            require_repository(repo)
            return backend.context(
                task=task,
                branch=branch,
                workspace=workspace,
                task_scope=task_scope,
                budget=budget,
            )

        return call("memory_context", task, operation)

    @mcp.tool(name="memory_search")
    def memory_search(query: str, repo: str, limit: int = 50) -> dict[str, Any]:
        """Search the assigned read-only benchmark memory condition."""

        def operation() -> dict[str, Any]:
            require_repository(repo)
            if not 1 <= limit <= 500:
                raise ValueError("limit must be between 1 and 500")
            return backend.search(query=query, limit=limit)

        return call("memory_search", query, operation)

    return mcp


def _selected_seed_ids(backend: ReadOnlyMemoryBackend, result: dict[str, Any]) -> list[str]:
    if isinstance(backend, MemoryOSBackend):
        return backend.manifest_seed_ids(result)
    manifest = result.get("manifest")
    if isinstance(manifest, list):
        return sorted(
            {
                str(item["memory_id"])
                for item in manifest
                if isinstance(item, dict) and item.get("included") is True and item.get("memory_id")
            }
        )
    items = result.get("items")
    if isinstance(items, list):
        return sorted(
            {
                str(item["memory"]["id"])
                for item in items
                if isinstance(item, dict)
                and isinstance(item.get("memory"), dict)
                and item["memory"].get("id")
            }
        )
    return []


def _lexical_score(query: str, seed: MemorySeedSpec) -> float:
    query_tokens = set(TOKEN_RE.findall(query.lower()))
    document_tokens = set(TOKEN_RE.findall(f"{seed.title} {seed.content}".lower()))
    return len(query_tokens & document_tokens) / max(1, len(query_tokens))


def _load_flat_backend(seed_file: Path, cutoff: datetime) -> FlatMemoryBackend:
    try:
        payload = json.loads(seed_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid flat-memory seed file: {seed_file}") from exc
    seeds = TypeAdapter(list[MemorySeedSpec]).validate_python(payload)
    return FlatMemoryBackend(seeds, cutoff)


def _parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("cutoff must include an explicit timezone")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        required=True,
        choices=[ExperimentCondition.FLAT_MEMORY.value, ExperimentCondition.MEMORYOS.value],
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--audit-file", type=Path, required=True)
    parser.add_argument("--seed-file", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    cutoff = _parse_cutoff(arguments.cutoff)
    if arguments.backend == ExperimentCondition.FLAT_MEMORY.value:
        if arguments.seed_file is None:
            parser.error("--seed-file is required for flat_memory")
        backend: ReadOnlyMemoryBackend = _load_flat_backend(arguments.seed_file, cutoff)
    else:
        if arguments.data_dir is None:
            parser.error("--data-dir is required for memoryos")
        backend = MemoryOSBackend(arguments.data_dir, arguments.repository, cutoff)
    auditor = ToolAuditor(arguments.audit_file, backend=arguments.backend)
    create_benchmark_mcp(
        backend,
        repository=arguments.repository,
        auditor=auditor,
        host=arguments.host,
        port=arguments.port,
    ).run(transport=arguments.transport)


if __name__ == "__main__":
    main()


__all__ = [
    "FlatMemoryBackend",
    "MemoryOSBackend",
    "ToolAuditor",
    "create_benchmark_mcp",
]
