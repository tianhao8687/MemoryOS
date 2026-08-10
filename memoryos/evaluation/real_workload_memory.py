from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import ClaimRow, ClaimVersionRow, MemoryRow, RetrievalRunRow
from memoryos.domain.schemas import (
    ClaimStatus,
    CreatedBy,
    MemoryCreate,
    MemoryStatus,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    MemoryExpectation,
    MemorySeedSpec,
    WorkloadTaskSpec,
)


class MemoryRuntimeError(RuntimeError):
    """Raised when a benchmark memory condition cannot be prepared or verified."""


@dataclass(frozen=True)
class MemoryRuntime:
    condition: ExperimentCondition
    config_path: Path
    audit_path: Path | None
    data_dir: Path | None
    seed_ids: tuple[str, ...]
    generated_memory_ids: dict[str, str]
    server_arguments: tuple[str, ...]


@dataclass(frozen=True)
class MemoryUsageEvidence:
    condition: ExperimentCondition
    valid: bool
    tool_calls: int
    retrieval_runs: int
    selected_seed_ids: tuple[str, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["condition"] = self.condition.value
        return payload


class MemoryRuntimeBuilder:
    def __init__(self, *, module: str = "memoryos.evaluation.real_workload_mcp") -> None:
        self.module = module

    def prepare(
        self,
        condition: ExperimentCondition,
        task: WorkloadTaskSpec,
        seeds: list[MemorySeedSpec],
        run_dir: Path,
        *,
        command: str = sys.executable,
        path_mapper: Callable[[Path], str] = str,
        python_path: str | None = None,
        http_url: str | None = None,
    ) -> MemoryRuntime:
        destination = run_dir.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        config_path = destination / "mcp.json"
        selected = _select_seeds(task, seeds)
        if condition is ExperimentCondition.NO_MEMORY:
            _write_json(config_path, {"mcpServers": {}})
            return MemoryRuntime(
                condition=condition,
                config_path=config_path,
                audit_path=None,
                data_dir=None,
                seed_ids=tuple(seed.id for seed in selected),
                generated_memory_ids={},
                server_arguments=(),
            )

        audit_path = destination / "memory-tool-audit.jsonl"
        common_arguments = [
            "-m",
            self.module,
            "--backend",
            condition.value,
            "--repository",
            task.repository_id,
            "--cutoff",
            task.cutoff.isoformat(),
            "--audit-file",
            path_mapper(audit_path),
        ]
        generated: dict[str, str] = {}
        data_dir: Path | None = None
        if condition is ExperimentCondition.FLAT_MEMORY:
            seed_path = destination / "flat-seeds.json"
            _write_json(
                seed_path,
                [seed.model_dump(mode="json", exclude_none=True) for seed in selected],
            )
            common_arguments.extend(["--seed-file", path_mapper(seed_path)])
        elif condition is ExperimentCondition.MEMORYOS:
            data_dir = destination / "memoryos-data"
            generated = seed_memoryos(data_dir, selected)
            common_arguments.extend(["--data-dir", path_mapper(data_dir)])
        else:  # pragma: no cover - enum exhaustiveness guard
            raise MemoryRuntimeError(f"unsupported memory condition: {condition}")

        environment = {"PYTHONUNBUFFERED": "1"}
        if python_path:
            environment["PYTHONPATH"] = python_path
        server_config: dict[str, Any]
        if http_url is None:
            server_config = {
                "command": command,
                "args": common_arguments,
                "env": environment,
            }
        else:
            if not http_url.startswith("http://"):
                raise MemoryRuntimeError("benchmark MCP URL must use isolated-network HTTP")
            server_config = {"url": http_url, "transport": "streamable-http"}
        _write_json(config_path, {"mcpServers": {"benchmark_memory": server_config}})
        return MemoryRuntime(
            condition=condition,
            config_path=config_path,
            audit_path=audit_path,
            data_dir=data_dir,
            seed_ids=tuple(seed.id for seed in selected),
            generated_memory_ids=generated,
            server_arguments=tuple(common_arguments),
        )

    def validate_usage(self, runtime: MemoryRuntime) -> MemoryUsageEvidence:
        if runtime.condition is ExperimentCondition.NO_MEMORY:
            return MemoryUsageEvidence(
                condition=runtime.condition,
                valid=True,
                tool_calls=0,
                retrieval_runs=0,
                selected_seed_ids=(),
                errors=(),
            )
        errors: list[str] = []
        audit = _read_audit(runtime.audit_path, errors)
        successful = [
            entry
            for entry in audit
            if entry.get("ok") is True and entry.get("backend") == runtime.condition.value
        ]
        selected = {
            str(seed_id)
            for entry in successful
            for seed_id in entry.get("selected_seed_ids", [])
            if isinstance(seed_id, str)
        }
        retrieval_runs = 0
        if runtime.condition is ExperimentCondition.MEMORYOS:
            if runtime.data_dir is None:
                errors.append("MemoryOS runtime has no data directory")
            else:
                database = Database(settings_for(runtime.data_dir))
                with database.session() as session:
                    runs = list(session.scalars(select(RetrievalRunRow)))
                    retrieval_runs = len(runs)
                    reverse_ids = {
                        value: key for key, value in runtime.generated_memory_ids.items()
                    }
                    for run in runs:
                        selected.update(
                            reverse_ids[memory_id]
                            for memory_id in run.selected_memory_ids
                            if memory_id in reverse_ids
                        )
                database.close()
            if retrieval_runs == 0:
                errors.append("MemoryOS condition produced no RetrievalRunRow")
        if not successful:
            errors.append("memory condition produced no successful MCP tool audit event")
        return MemoryUsageEvidence(
            condition=runtime.condition,
            valid=not errors,
            tool_calls=len(audit),
            retrieval_runs=retrieval_runs,
            selected_seed_ids=tuple(sorted(selected)),
            errors=tuple(errors),
        )


def seed_memoryos(data_dir: Path, seeds: list[MemorySeedSpec]) -> dict[str, str]:
    settings = settings_for(data_dir)
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    generated: dict[str, str] = {}
    for seed in seeds:
        active = seed.expectation is not MemoryExpectation.STALE
        source_type = SourceType.GIT_COMMIT if seed.source_commit else SourceType.FILE_REFERENCE
        source_ref = (
            f"git:{seed.source_commit}:{seed.source_ref}" if seed.source_commit else seed.source_ref
        )
        result = service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key=seed.repository_id,
                memory_type=seed.memory_type,
                category=seed.category,
                title=seed.title,
                content=seed.content,
                confidence=seed.confidence,
                importance=seed.importance,
                valid_from=seed.valid_from,
                valid_to=seed.valid_to,
                created_by=CreatedBy.MANUAL if active else CreatedBy.IMPORT,
                metadata={
                    "benchmark_seed_id": seed.id,
                    "benchmark_expectation": seed.expectation.value,
                },
                source=SourceCreate(
                    source_type=source_type,
                    source_ref=source_ref,
                    captured_at=seed.captured_at,
                    excerpt=seed.content[:10_000],
                    metadata={"benchmark_seed_id": seed.id},
                ),
                activate_immediately=active,
            ),
            actor="benchmark-seed",
        )
        memory_id = str(result["id"])
        generated[seed.id] = memory_id
        _backdate_seed(database, memory_id, seed.captured_at)
        if not active:
            _mark_historical_seed(database, memory_id)
    database.close()
    return generated


def _backdate_seed(database: Database, memory_id: str, captured_at: datetime) -> None:
    """Represent when imported evidence was knowable, not when the replay DB was created."""
    with database.session() as session:
        memory = session.get(MemoryRow, memory_id)
        if memory is None:  # pragma: no cover - service just created it transactionally
            raise MemoryRuntimeError(f"seeded memory disappeared: {memory_id}")
        memory.created_at = captured_at
        memory.updated_at = captured_at
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            claim.recorded_at = captured_at
        for version in session.scalars(
            select(ClaimVersionRow).where(ClaimVersionRow.memory_id == memory_id)
        ):
            version.transaction_from = captured_at
            version.created_at = captured_at


def _mark_historical_seed(database: Database, memory_id: str) -> None:
    with database.session() as session:
        memory = session.get(MemoryRow, memory_id)
        if memory is None:  # pragma: no cover - service just created it transactionally
            raise MemoryRuntimeError(f"seeded memory disappeared: {memory_id}")
        memory.status = MemoryStatus.EXPIRED
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            claim.status = ClaimStatus.HISTORICAL


def _select_seeds(task: WorkloadTaskSpec, seeds: list[MemorySeedSpec]) -> list[MemorySeedSpec]:
    indexed = {seed.id: seed for seed in seeds}
    try:
        return [indexed[seed_id] for seed_id in task.memory_seed_ids]
    except KeyError as exc:
        raise MemoryRuntimeError(f"task references missing seed: {exc.args[0]}") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_audit(path: Path | None, errors: list[str]) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    if path.stat().st_size > 10 * 1024 * 1024:
        errors.append("memory tool audit exceeds 10 MiB")
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"invalid audit JSON on line {line_number}")
            continue
        if isinstance(value, dict):
            entries.append(value)
        else:
            errors.append(f"audit line {line_number} is not an object")
    return entries


__all__ = [
    "MemoryRuntime",
    "MemoryRuntimeBuilder",
    "MemoryRuntimeError",
    "MemoryUsageEvidence",
    "seed_memoryos",
]
