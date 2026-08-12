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
from memoryos.retrieval_v2.pipeline import retrieval_config_hash
from memoryos.retrieval_v2.rrf_shadow import RRFChannelShadowProfile
from memoryos.retrieval_v2.scoring import ShadowRetrievalProfile

_PUBLISHABLE_RETRIEVAL_FEATURES = frozenset(
    {
        "fts_rank",
        "vector_rank",
        "graph_rank",
        "temporal_rank",
        "scope_match",
        "freshness",
        "truth_state",
        "evidence_count",
        "helpful_feedback_count",
        "unhelpful_feedback_count",
        "memory_confidence",
        "memory_importance",
        "reranker_score",
    }
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
    scoring_profile_sha256: str | None
    expected_retrieval_config_hash: str | None
    server_arguments: tuple[str, ...]
    server_environment: tuple[tuple[str, str], ...]
    embedding_model: str | None


@dataclass(frozen=True)
class MemoryUsageEvidence:
    condition: ExperimentCondition
    valid: bool
    tool_calls: int
    retrieval_runs: int
    selected_seed_ids: tuple[str, ...]
    candidate_features: tuple[dict[str, Any], ...]
    retrieval_config_hashes: tuple[str, ...]
    scoring_profile_sha256: str | None
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
        scoring_profile: ShadowRetrievalProfile | None = None,
        rrf_channel_profile: RRFChannelShadowProfile | None = None,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
    ) -> MemoryRuntime:
        destination = run_dir.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        config_path = destination / "mcp.json"
        selected = _select_seeds(task, seeds)
        if scoring_profile is not None and rrf_channel_profile is not None:
            raise MemoryRuntimeError("benchmark can use only one shadow profile")
        if (scoring_profile is not None or rrf_channel_profile is not None) and (
            condition is not ExperimentCondition.MEMORYOS
        ):
            raise MemoryRuntimeError("shadow retrieval profiles are valid only for MemoryOS")
        if (embedding_base_url is None) != (embedding_model is None):
            raise MemoryRuntimeError("embedding_base_url and embedding_model must be set together")
        if rrf_channel_profile is not None:
            if embedding_model is None:
                raise MemoryRuntimeError("RRF channel shadow requires its real embedding provider")
            expected_prefix = f"fastembed:{embedding_model}@"
            if not rrf_channel_profile.source_vector_channel_id.startswith(expected_prefix):
                raise MemoryRuntimeError(
                    "embedding_model does not match the RRF shadow source vector channel"
                )
        server_environment = {"PYTHONUNBUFFERED": "1"}
        if embedding_base_url is not None and embedding_model is not None:
            server_environment.update(
                {
                    "MEMORYOS_EMBEDDING_BASE_URL": embedding_base_url,
                    "MEMORYOS_EMBEDDING_MODEL": embedding_model,
                    "MEMORYOS_ANN_ENABLED": "false",
                }
            )
        if condition is ExperimentCondition.NO_MEMORY:
            _write_json(config_path, {"mcpServers": {}})
            return MemoryRuntime(
                condition=condition,
                config_path=config_path,
                audit_path=None,
                data_dir=None,
                seed_ids=tuple(seed.id for seed in selected),
                generated_memory_ids={},
                scoring_profile_sha256=None,
                expected_retrieval_config_hash=None,
                server_arguments=(),
                server_environment=(),
                embedding_model=None,
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
            if scoring_profile is not None:
                profile_path = destination / "shadow-retrieval-profile.json"
                _write_json(profile_path, scoring_profile.model_dump(mode="json"))
                common_arguments.extend(["--weight-profile", path_mapper(profile_path)])
            if rrf_channel_profile is not None:
                profile_path = destination / "rrf-channel-shadow-profile.json"
                _write_json(profile_path, rrf_channel_profile.model_dump(mode="json"))
                common_arguments.extend(["--rrf-channel-profile", path_mapper(profile_path)])
                common_arguments.extend(
                    [
                        "--expected-vector-channel-id",
                        rrf_channel_profile.source_vector_channel_id,
                        "--expected-vector-channel-source-sha256",
                        rrf_channel_profile.source_vector_channel_sha256,
                        "--expected-vector-feature-adapter-sha256",
                        rrf_channel_profile.source_vector_adapter_sha256,
                    ]
                )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise MemoryRuntimeError(f"unsupported memory condition: {condition}")

        environment = dict(server_environment)
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
            scoring_profile_sha256=(
                scoring_profile.digest()
                if scoring_profile is not None
                else rrf_channel_profile.digest()
                if rrf_channel_profile is not None
                else None
            ),
            expected_retrieval_config_hash=(
                retrieval_config_hash(
                    scoring_profile,
                    rrf_channel_profile=rrf_channel_profile,
                )
                if condition is ExperimentCondition.MEMORYOS
                else None
            ),
            server_arguments=tuple(common_arguments),
            server_environment=tuple(sorted(server_environment.items())),
            embedding_model=embedding_model,
        )

    def validate_usage(self, runtime: MemoryRuntime) -> MemoryUsageEvidence:
        if runtime.condition is ExperimentCondition.NO_MEMORY:
            return MemoryUsageEvidence(
                condition=runtime.condition,
                valid=True,
                tool_calls=0,
                retrieval_runs=0,
                selected_seed_ids=(),
                candidate_features=(),
                retrieval_config_hashes=(),
                scoring_profile_sha256=None,
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
        candidate_features: list[dict[str, Any]] = []
        retrieval_config_hashes: set[str] = set()
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
                    memory_rows = {
                        memory_id: session.get(MemoryRow, memory_id)
                        for memory_id in runtime.generated_memory_ids.values()
                    }
                    for retrieval_index, run in enumerate(runs):
                        retrieval_config_hashes.add(run.config_hash)
                        selected.update(
                            reverse_ids[memory_id]
                            for memory_id in run.selected_memory_ids
                            if memory_id in reverse_ids
                        )
                        for item in run.candidate_features:
                            memory_id = str(item.get("memory_id", ""))
                            seed_id = reverse_ids.get(memory_id)
                            if seed_id is None:
                                continue
                            trace = {
                                str(key): value
                                for key, value in item.items()
                                if key in _PUBLISHABLE_RETRIEVAL_FEATURES
                            }
                            memory_row = memory_rows.get(memory_id)
                            if memory_row is not None:
                                trace.setdefault(
                                    "memory_confidence",
                                    float(memory_row.confidence),
                                )
                                trace.setdefault(
                                    "memory_importance",
                                    float(memory_row.importance),
                                )
                            candidate_features.append(
                                {
                                    "seed_id": seed_id,
                                    "retrieval_index": retrieval_index,
                                    "selected": memory_id in run.selected_memory_ids,
                                    "trace": trace,
                                }
                            )
                database.close()
            if retrieval_runs == 0:
                errors.append("MemoryOS condition produced no RetrievalRunRow")
            if runtime.expected_retrieval_config_hash is None or retrieval_config_hashes != {
                runtime.expected_retrieval_config_hash
            }:
                errors.append(
                    "MemoryOS RetrievalRun config hash does not match its scoring profile"
                )
            if (
                runtime.embedding_model is not None
                and runtime.seed_ids
                and not any(
                    item["trace"].get("vector_rank") is not None for item in candidate_features
                )
            ):
                errors.append("MemoryOS run did not produce real vector retrieval evidence")
        if not successful:
            errors.append("memory condition produced no successful MCP tool audit event")
        return MemoryUsageEvidence(
            condition=runtime.condition,
            valid=not errors,
            tool_calls=len(audit),
            retrieval_runs=retrieval_runs,
            selected_seed_ids=tuple(sorted(selected)),
            candidate_features=tuple(candidate_features),
            retrieval_config_hashes=tuple(sorted(retrieval_config_hashes)),
            scoring_profile_sha256=runtime.scoring_profile_sha256,
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
