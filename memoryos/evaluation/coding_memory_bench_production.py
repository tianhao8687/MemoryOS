from __future__ import annotations

import hashlib
import html
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from memoryos.claims.truth import TruthMaintenanceService
from memoryos.config import settings_for
from memoryos.db.models import RetrievalRunRow
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimStaleState,
    ConflictStrategy,
    ContextRequest,
    CreatedBy,
    CurrentTruthRequest,
    EntityType,
    EvidenceSpan,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.retrieval_v2 import RetrievalPipeline

FORBIDDEN_GOLD_KEYS = {"gold", "expected", "answer", "target_ids", "label"}


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _contains_gold_key(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(FORBIDDEN_GOLD_KEYS.intersection(value)) or any(
            _contains_gold_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_gold_key(item) for item in value)
    return False


def _repository_commit(repository_root: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed local git metadata command
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unavailable"


class ProductionCodingMemoryBench:
    """Small integration benchmark that executes MemoryOS's real local production path."""

    VERSION = "coding-memory-bench-production@1"

    def run(self, data_dir: Path) -> dict[str, Any]:
        settings = settings_for(
            data_dir,
            embedding_base_url=None,
            embedding_model=None,
            extractor_base_url=None,
            extractor_model=None,
            relationship_model=None,
            reranker_model=None,
            consolidation_model=None,
        )
        if settings.database_path.exists():
            raise ValueError("production benchmark requires a fresh data directory")
        database = Database(settings)
        database.initialize()
        service = MemoryService(database, settings)
        try:
            if not isinstance(service.retrieval_v2, RetrievalPipeline):
                raise AssertionError("production retrieval pipeline is not active")
            if not isinstance(service.truth, TruthMaintenanceService):
                raise AssertionError("production current-truth service is not active")

            runtime, gold = self._seed(service)
            if _contains_gold_key(runtime):
                raise AssertionError("production runtime payload leaked a gold field")

            retrieval_outputs: dict[str, list[str]] = {}
            context_outputs: dict[str, list[str]] = {}
            temporal_outputs: dict[str, dict[str, Any]] = {}
            conflict_outputs: dict[str, bool] = {}
            requested_channels: set[str] = set()
            executed_channels: set[str] = set()
            contributing_channels: set[str] = set()
            degraded_channels: set[str] = set()
            channel_execution: dict[str, dict[str, Any]] = {}

            for case in runtime["retrieval"]:
                result = service.search(
                    SearchRequest(
                        query=str(case["query"]),
                        scope_type=ScopeType(str(case["scope_type"])),
                        scope_key=str(case["scope_key"]),
                        limit=5,
                    )
                )
                retrieval_outputs[str(case["id"])] = [
                    str(item["memory"]["id"]) for item in result["items"]
                ]
                routing = result["query_plan"]["routing"]
                requested_channels.update(str(item) for item in routing["requested_channels"])
                executed_channels.update(str(item) for item in routing["executed_channels"])
                contributing_channels.update(str(item) for item in routing["contributing_channels"])
                degraded_channels.update(str(item) for item in routing["degraded_channels"])
                for item in routing["channel_execution"]:
                    channel_execution[str(item["channel"])] = dict(item)

            for case in runtime["context"]:
                result = service.context(
                    ContextRequest(
                        task=str(case["task"]),
                        repository=str(case["scope_key"]),
                        budget=int(case["budget"]),
                    )
                )
                context_outputs[str(case["id"])] = sorted(
                    str(item["memory_id"]) for item in result["manifest"] if item["included"]
                )

            for case in runtime["temporal"]:
                result = service.current_truth(
                    CurrentTruthRequest(
                        scope_type=ScopeType(str(case["scope_type"])),
                        scope_key=str(case["scope_key"]),
                        subject=str(case["subject"]),
                        predicate=str(case["predicate"]),
                        as_of_valid_time=datetime.fromisoformat(str(case["as_of_valid_time"])),
                        as_known_at=datetime.fromisoformat(str(case["as_known_at"])),
                    )
                )
                accepted = result["accepted_claims"]
                temporal_outputs[str(case["id"])] = {
                    "state": str(result["state"]),
                    "object_values": sorted({str(item["object_value"]) for item in accepted}),
                    "memory_ids": sorted({str(item["memory_id"]) for item in accepted}),
                }

            for case in runtime["conflict"]:
                result = service.current_truth(
                    CurrentTruthRequest(
                        scope_type=ScopeType(str(case["scope_type"])),
                        scope_key=str(case["scope_key"]),
                        subject=str(case["subject"]),
                        predicate=str(case["predicate"]),
                    )
                )
                conflict_outputs[str(case["id"])] = result["state"] == "contested"

            retrieval_score = _mean(
                [
                    1.0
                    if set(gold["retrieval"][identity]) & set(retrieval_outputs[identity][:5])
                    else 0.0
                    for identity in retrieval_outputs
                ]
            )
            context_score = _mean(
                [
                    1.0 if str(expected) in context_outputs[identity] else 0.0
                    for identity, expected in gold["context"].items()
                ]
            )
            temporal_case_results = {
                identity: temporal_outputs[identity] == expected
                for identity, expected in gold["temporal"].items()
            }
            temporal_score = _mean(
                [1.0 if passed else 0.0 for passed in temporal_case_results.values()]
            )
            conflict_score = _mean(
                [
                    1.0 if conflict_outputs[identity] is expected else 0.0
                    for identity, expected in gold["conflict"].items()
                ]
            )
            with database.session() as session:
                retrieval_run_count = int(
                    session.scalar(select(func.count()).select_from(RetrievalRunRow)) or 0
                )
            production_path_executed = (
                retrieval_run_count == len(runtime["retrieval"]) + len(runtime["context"])
                and len(context_outputs) == len(runtime["context"])
                and len(temporal_outputs) == len(runtime["temporal"])
                and len(conflict_outputs) == len(runtime["conflict"])
            )
            if not production_path_executed:
                raise AssertionError("production benchmark did not execute every requested path")

            repository_root = Path(__file__).resolve().parents[2]
            return {
                "schema": self.VERSION,
                "generated_at": datetime.now(UTC).isoformat(),
                "evidence_type": "local_production_path_integration",
                "effect_claim": "integration_correctness_only",
                "production_path_executed": True,
                "blind_protocol": {
                    "runtime_payload_contains_gold": False,
                    "gold_loaded_only_by_scorer": True,
                    "immutable_input_hash": _hash(runtime),
                    "immutable_gold_hash": _hash(gold),
                },
                "provenance": {
                    "repository_commit": _repository_commit(repository_root),
                    "retrieval_config_hash": service.retrieval_v2.config_hash,
                    "database_schema_version": database.schema_version(),
                    "providers": "deterministic_local_core",
                },
                "active_modules": [
                    "memoryos.db.session.Database",
                    "memoryos.engine.service.MemoryService",
                    "memoryos.retrieval_v2.pipeline.RetrievalPipeline",
                    "memoryos.context.compiler.TaskAwareContextCompiler",
                    "memoryos.claims.truth.TruthMaintenanceService.current_truth",
                ],
                "retrieval": {
                    "recall_at_5": retrieval_score,
                    "requested_channels": sorted(requested_channels),
                    "executed_channels": sorted(executed_channels),
                    "contributing_channels": sorted(contributing_channels),
                    "degraded_channels": sorted(degraded_channels),
                    "channel_execution": [
                        channel_execution[key] for key in sorted(channel_execution)
                    ],
                    "retrieval_run_count": retrieval_run_count,
                },
                "context": {
                    "target_inclusion_rate": context_score,
                    "executed_cases": len(context_outputs),
                },
                "temporal": {
                    "accuracy": temporal_score,
                    "case_results": temporal_case_results,
                    "outputs": temporal_outputs,
                },
                "conflict": {"accuracy": conflict_score},
                "sample_sizes": {key: len(value) for key, value in runtime.items()},
                "coverage": {
                    "retrieval_hard_negatives": [
                        "sibling_scope",
                        "stale",
                        "candidate",
                        "archived",
                        "negative_constraint",
                        "target",
                    ],
                    "temporal": [
                        "valid_time",
                        "known_time",
                        "superseded_history",
                        "stale",
                        "archive_restore_history",
                    ],
                    "conflict": ["contested", "resolved"],
                },
                "gates": {
                    "retrieval_recall_at_5": retrieval_score == 1.0,
                    "context_target_inclusion": context_score == 1.0,
                    "temporal_accuracy": temporal_score == 1.0,
                    "conflict_accuracy": conflict_score == 1.0,
                    "gold_isolation": True,
                    "production_path_executed": True,
                },
                "limitations": [
                    (
                        "Small deterministic integration corpus; it does not estimate "
                        "real-agent effect."
                    ),
                    "External embeddings, rerankers, and model judges are intentionally disabled.",
                ],
            }
        finally:
            service.close()
            database.close()

    def write(self, report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "coding-memory-bench-production.json"
        html_path = output_dir / "coding-memory-bench-production.html"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>CodingMemoryBench Production Path</title>"
            "<style>body{font:16px system-ui;max-width:960px;margin:40px auto;color:#18211d}"
            "code{background:#eef4f0;padding:2px 5px}</style>"
            "<h1>CodingMemoryBench — production-path integration</h1>"
            f"<p>Evidence: <code>{html.escape(str(report['evidence_type']))}</code>; "
            f"effect claim: <code>{html.escape(str(report['effect_claim']))}</code>.</p>"
            f"<p>Retrieval Recall@5: {report['retrieval']['recall_at_5']:.3f}; "
            f"temporal accuracy: {report['temporal']['accuracy']:.3f}; "
            f"conflict accuracy: {report['conflict']['accuracy']:.3f}.</p>",
            encoding="utf-8",
        )
        return {"json": json_path, "html": html_path}

    def _seed(
        self,
        service: MemoryService,
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
        runtime: dict[str, list[dict[str, Any]]] = {
            "retrieval": [],
            "context": [],
            "temporal": [],
            "conflict": [],
        }
        gold: dict[str, dict[str, Any]] = {
            "retrieval": {},
            "context": {},
            "temporal": {},
            "conflict": {},
        }
        context_target: dict[str, Any] | None = None
        for index in range(4):
            case_id = f"production-retrieval-{index}"
            scope_key = f"production-repo-{index}"
            needle = f"prodneedle{index}"
            target = service.propose(
                self._memory(
                    scope_key=scope_key,
                    title=f"Confirmed cache decision {index}",
                    content=f"The current production cache decision is {needle} confirmed.",
                    key=f"benchmark.target.{index}",
                    active=True,
                ),
                actor="benchmark-seed",
            )
            stale = service.propose(
                self._memory(
                    scope_key=scope_key,
                    title=f"Obsolete cache decision {index}",
                    content=f"The current production cache decision is {needle} obsolete.",
                    key=f"benchmark.stale.{index}",
                    active=True,
                ),
                actor="benchmark-seed",
            )
            with service.database.session() as session:
                service.truth.mark_memory_stale(
                    session,
                    str(stale["id"]),
                    ClaimStaleState.STALE,
                    actor="benchmark-seed",
                )
            service.propose(
                self._memory(
                    scope_key=f"{scope_key}-sibling",
                    title=f"Cross-scope cache decision {index}",
                    content=f"The current production cache decision is {needle} sibling.",
                    key=f"benchmark.cross_scope.{index}",
                    active=True,
                ),
                actor="benchmark-seed",
            )
            service.propose(
                self._memory(
                    scope_key=scope_key,
                    title=f"Unconfirmed cache decision {index}",
                    content=f"The current production cache decision is {needle} candidate.",
                    key=f"benchmark.candidate.{index}",
                    active=False,
                ),
                actor="benchmark-seed",
            )
            archived = service.propose(
                self._memory(
                    scope_key=scope_key,
                    title=f"Archived cache decision {index}",
                    content=f"The archived production cache decision was {needle} archived.",
                    key=f"benchmark.archived.{index}",
                    active=False,
                ),
                actor="benchmark-seed",
            )
            service.archive_memory(str(archived["id"]), actor="benchmark-seed")
            service.propose(
                self._memory(
                    scope_key=scope_key,
                    title=f"Negative cache constraint {index}",
                    content=f"Do not use {needle} as an obsolete production cache decision.",
                    key=f"benchmark.negative_constraint.{index}",
                    active=True,
                ),
                actor="benchmark-seed",
            )
            runtime["retrieval"].append(
                {
                    "id": case_id,
                    "query": f"current production cache decision {needle}",
                    "scope_type": ScopeType.REPOSITORY.value,
                    "scope_key": scope_key,
                }
            )
            gold["retrieval"][case_id] = [str(target["id"])]
            if index == 0:
                context_target = target

        if context_target is None:
            raise AssertionError("production benchmark did not seed a context target")
        runtime["context"].append(
            {
                "id": "production-context-0",
                "task": "implement current production cache decision prodneedle0",
                "scope_key": "production-repo-0",
                "budget": 3000,
            }
        )
        gold["context"]["production-context-0"] = str(context_target["id"])

        now = datetime.now(UTC)
        split = now - timedelta(days=15)
        temporal_scope = "production-temporal"
        temporal_subject = "project.production_database"
        historical_temporal = self._propose_claim_memory(
            service,
            scope_key=temporal_scope,
            title="Historical production database",
            content="The production database used sqlite during the first interval.",
            subject=temporal_subject,
            value="sqlite",
            valid_from=now - timedelta(days=30),
            valid_to=split,
        )
        current_temporal = self._propose_claim_memory(
            service,
            scope_key=temporal_scope,
            title="Current production database",
            content="The production database uses postgresql during the second interval.",
            subject=temporal_subject,
            value="postgresql",
            valid_from=split,
            valid_to=None,
        )
        # Freeze transaction-time after seeding but before Current Truth performs its
        # wall-clock expiry sweep. This keeps the historical snapshot reproducible.
        known_at = datetime.now(UTC)
        for case_id, moment, expected in (
            ("production-temporal-old", now - timedelta(days=20), "sqlite"),
            ("production-temporal-current", now - timedelta(days=5), "postgresql"),
        ):
            runtime["temporal"].append(
                {
                    "id": case_id,
                    "scope_type": ScopeType.REPOSITORY.value,
                    "scope_key": temporal_scope,
                    "subject": temporal_subject,
                    "predicate": "uses",
                    "as_of_valid_time": moment.isoformat(),
                    "as_known_at": known_at.isoformat(),
                }
            )
            expected_memory = (
                str(historical_temporal["id"])
                if expected == "sqlite"
                else str(current_temporal["id"])
            )
            gold["temporal"][case_id] = {
                "state": "resolved",
                "object_values": [expected],
                "memory_ids": [expected_memory],
            }

        transition_scope = "production-transition-history"
        transition_subject = "project.transition_database"
        superseded = self._propose_claim_memory(
            service,
            scope_key=transition_scope,
            title="Original transition database",
            content="The transition database uses mysql.",
            subject=transition_subject,
            value="mysql",
        )
        successor = self._propose_claim_memory(
            service,
            scope_key=transition_scope,
            title="Replacement transition database",
            content="The transition database uses postgresql.",
            subject=transition_subject,
            value="postgresql",
            active=False,
        )
        before_supersede = datetime.now(UTC)
        service.confirm(
            str(successor["id"]),
            strategy=ConflictStrategy.SUPERSEDE,
            actor="benchmark-seed",
            rationale="Production-path supersede fixture",
        )
        after_supersede = datetime.now(UTC)
        for case_id, known_at, value, memory_id in (
            (
                "production-temporal-before-supersede",
                before_supersede,
                "mysql",
                str(superseded["id"]),
            ),
            (
                "production-temporal-after-supersede",
                after_supersede,
                "postgresql",
                str(successor["id"]),
            ),
        ):
            runtime["temporal"].append(
                {
                    "id": case_id,
                    "scope_type": ScopeType.REPOSITORY.value,
                    "scope_key": transition_scope,
                    "subject": transition_subject,
                    "predicate": "uses",
                    "as_of_valid_time": after_supersede.isoformat(),
                    "as_known_at": known_at.isoformat(),
                }
            )
            gold["temporal"][case_id] = {
                "state": "resolved",
                "object_values": [value],
                "memory_ids": [memory_id],
            }

        stale_scope = "production-stale-history"
        stale_subject = "project.stale_database"
        stale_memory = self._propose_claim_memory(
            service,
            scope_key=stale_scope,
            title="Stale database decision",
            content="The stale database uses sqlite.",
            subject=stale_subject,
            value="sqlite",
        )
        with service.database.session() as session:
            service.truth.mark_memory_stale(
                session,
                str(stale_memory["id"]),
                ClaimStaleState.STALE,
                actor="benchmark-seed",
            )
        stale_known_at = datetime.now(UTC)
        runtime["temporal"].append(
            {
                "id": "production-temporal-stale",
                "scope_type": ScopeType.REPOSITORY.value,
                "scope_key": stale_scope,
                "subject": stale_subject,
                "predicate": "uses",
                "as_of_valid_time": stale_known_at.isoformat(),
                "as_known_at": stale_known_at.isoformat(),
            }
        )
        gold["temporal"]["production-temporal-stale"] = {
            "state": "stale",
            "object_values": [],
            "memory_ids": [],
        }

        archive_scope = "production-archive-history"
        archive_subject = "project.archive_database"
        archive_primary = self._propose_claim_memory(
            service,
            scope_key=archive_scope,
            title="Primary archive database support",
            content="The archive database uses sqlite from primary evidence.",
            subject=archive_subject,
            value="sqlite",
        )
        archive_alternative = self._propose_claim_memory(
            service,
            scope_key=archive_scope,
            title="Alternative archive database support",
            content="The archive database uses sqlite from independent evidence.",
            subject=archive_subject,
            value="sqlite",
            key_suffix="alternative",
        )
        archived_result = service.archive_memory(
            str(archive_primary["id"]),
            actor="benchmark-seed",
        )
        during_archive = datetime.fromisoformat(str(archived_result["archived_at"]))
        restored_result = service.restore_archived_memory(
            str(archive_primary["id"]),
            actor="benchmark-seed",
        )
        after_restore = datetime.fromisoformat(str(restored_result["evaluated_at"]))
        for case_id, known_at, memory_ids in (
            (
                "production-temporal-during-archive",
                during_archive,
                [str(archive_alternative["id"])],
            ),
            (
                "production-temporal-after-restore",
                after_restore,
                sorted([str(archive_primary["id"]), str(archive_alternative["id"])]),
            ),
        ):
            runtime["temporal"].append(
                {
                    "id": case_id,
                    "scope_type": ScopeType.REPOSITORY.value,
                    "scope_key": archive_scope,
                    "subject": archive_subject,
                    "predicate": "uses",
                    "as_of_valid_time": after_restore.isoformat(),
                    "as_known_at": known_at.isoformat(),
                }
            )
            gold["temporal"][case_id] = {
                "state": "resolved",
                "object_values": ["sqlite"],
                "memory_ids": sorted(memory_ids),
            }

        conflict_scope = "production-conflict"
        conflict_subject = "project.runtime_database"
        self._propose_claim_memory(
            service,
            scope_key=conflict_scope,
            title="Runtime database A",
            content="The runtime database uses sqlite.",
            subject=conflict_subject,
            value="sqlite",
        )
        conflicting = self._propose_claim_memory(
            service,
            scope_key=conflict_scope,
            title="Runtime database B",
            content="The runtime database uses postgresql.",
            subject=conflict_subject,
            value="postgresql",
            active=False,
        )
        service.confirm(
            str(conflicting["id"]),
            strategy=ConflictStrategy.KEEP_BOTH,
            actor="benchmark-seed",
            rationale="Production-path conflict fixture",
        )
        resolved_subject = "project.queue_backend"
        self._propose_claim_memory(
            service,
            scope_key=conflict_scope,
            title="Queue backend",
            content="The queue backend uses redis.",
            subject=resolved_subject,
            value="redis",
        )
        for case_id, subject, conflict_expected in (
            ("production-conflict-contested", conflict_subject, True),
            ("production-conflict-resolved", resolved_subject, False),
        ):
            runtime["conflict"].append(
                {
                    "id": case_id,
                    "scope_type": ScopeType.REPOSITORY.value,
                    "scope_key": conflict_scope,
                    "subject": subject,
                    "predicate": "uses",
                }
            )
            gold["conflict"][case_id] = conflict_expected
        return runtime, gold

    @staticmethod
    def _memory(
        *,
        scope_key: str,
        title: str,
        content: str,
        key: str,
        active: bool,
    ) -> MemoryCreate:
        return MemoryCreate(
            scope_type=ScopeType.REPOSITORY,
            scope_key=scope_key,
            memory_type=MemoryType.PROJECT,
            category="decision",
            key=key,
            title=title,
            content=content,
            created_by=CreatedBy.MANUAL,
            activate_immediately=active,
            source=SourceCreate(
                source_type=SourceType.MANUAL,
                source_ref=f"production-bench:{scope_key}:{key}",
                excerpt=content,
            ),
        )

    @staticmethod
    def _propose_claim_memory(
        service: MemoryService,
        *,
        scope_key: str,
        title: str,
        content: str,
        subject: str,
        value: str,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        active: bool = True,
        key_suffix: str | None = None,
    ) -> dict[str, Any]:
        return service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key=scope_key,
                memory_type=MemoryType.PROJECT,
                category="decision",
                key=(
                    f"benchmark.{subject}.{value}.{key_suffix}"
                    if key_suffix
                    else f"benchmark.{subject}.{value}"
                ),
                title=title,
                content=content,
                valid_from=valid_from,
                valid_to=valid_to,
                created_by=CreatedBy.MANUAL,
                activate_immediately=active,
                claim_candidates=[
                    ClaimCandidate(
                        subject_hint=subject,
                        subject_type=EntityType.PROJECT,
                        predicate="uses",
                        object_kind=ClaimObjectKind.LITERAL,
                        object_value=value,
                        polarity=ClaimPolarity.POSITIVE,
                        modality=ClaimModality.DECISION,
                        confidence=0.95,
                        evidence_span=EvidenceSpan(start=0, end=len(content), quote=content),
                    )
                ],
                source=SourceCreate(
                    source_type=SourceType.MANUAL,
                    source_ref=f"production-bench:{scope_key}:{subject}:{value}",
                    excerpt=content,
                ),
            ),
            actor="benchmark-seed",
        )


__all__ = ["ProductionCodingMemoryBench"]
