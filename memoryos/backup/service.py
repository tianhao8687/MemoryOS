from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from memoryos.config import MemoryOSSettings
from memoryos.db.models import (
    AuditEventRow,
    ClaimEvidenceRow,
    ClaimIdentityRow,
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    ConsolidationCandidateRow,
    EmbeddingRow,
    EntityMergeEventRow,
    EntityRow,
    MemoryFeedbackRow,
    MemoryHealthRow,
    MemoryRow,
    MemorySourceRow,
    PossibleConflictRow,
    RelationRow,
    RepositoryRow,
    RetrievalRunRow,
    SettingRow,
    SourceAnchorRow,
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
    CreatedBy,
    EntityType,
    FeedbackValue,
    FreshnessState,
    MemoryStatus,
    MemoryTemperature,
    MemoryType,
    PossibleConflictStatus,
    RelationMethod,
    ScopeType,
    Sensitivity,
    SourceType,
)
from memoryos.errors import BackupError

FORMAT_VERSION = 3
SUPPORTED_IMPORT_VERSIONS = {1, 2, FORMAT_VERSION}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BackupService:
    def __init__(self, database: Database, settings: MemoryOSSettings) -> None:
        self.database = database
        self.settings = settings

    def create_backup(self, destination: Path | None = None) -> Path:
        self.database.checkpoint()
        if destination is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            destination = self.settings.backup_dir / f"memoryos-{stamp}.zip"
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        database_bytes = self.settings.database_path.read_bytes()
        manifest = {
            "format": "memoryos-sqlite-backup",
            "format_version": FORMAT_VERSION,
            "schema_version": self.database.schema_version(),
            "created_at": datetime.now(UTC).isoformat(),
            "database_sha256": _sha256(database_bytes),
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.writestr("memoryos.db", database_bytes)
        return destination

    def restore(self, archive_path: Path, *, create_safety_backup: bool = True) -> Path | None:
        archive_path = archive_path.expanduser().resolve()
        if not archive_path.is_file():
            raise BackupError(f"backup archive does not exist: {archive_path}")
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if names != {"manifest.json", "memoryos.db"}:
                raise BackupError("backup contains unexpected or missing entries")
            manifest = json.loads(archive.read("manifest.json"))
            database_bytes = archive.read("memoryos.db")
        if (
            manifest.get("format") != "memoryos-sqlite-backup"
            or manifest.get("format_version") not in SUPPORTED_IMPORT_VERSIONS
        ):
            raise BackupError("unsupported backup format")
        if not isinstance(manifest.get("database_sha256"), str) or not hmac.compare_digest(
            manifest["database_sha256"], _sha256(database_bytes)
        ):
            raise BackupError("backup integrity hash does not match")

        with tempfile.NamedTemporaryFile(
            suffix=".db", dir=self.settings.data_dir, delete=False
        ) as temporary:
            temporary.write(database_bytes)
            source_path = Path(temporary.name)
        try:
            try:
                source = sqlite3.connect(source_path)
                try:
                    integrity = source.execute("PRAGMA integrity_check").fetchone()
                    if not integrity or integrity[0] != "ok":
                        raise BackupError("restored database failed integrity_check")
                    tables = {
                        row[0]
                        for row in source.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "memories" not in tables or "alembic_version" not in tables:
                        raise BackupError("backup is not a MemoryOS database")
                    safety = self.create_backup() if create_safety_backup else None
                    self.database.close()
                    target = sqlite3.connect(self.settings.database_path)
                    try:
                        source.backup(target)
                        target.commit()
                    finally:
                        target.close()
                    self.database.initialize()
                    return safety
                finally:
                    source.close()
            except sqlite3.DatabaseError as exc:
                raise BackupError("backup database is corrupt or unreadable") from exc
        finally:
            source_path.unlink(missing_ok=True)

    def export_jsonl(self, destination: Path) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        with self.database.session() as session:
            for repository in session.scalars(select(RepositoryRow)):
                self._append(lines, "repository", self._repository(repository))
            for source in session.scalars(select(SourceRow)):
                self._append(lines, "source", self._source(source))
            for memory in session.scalars(select(MemoryRow)):
                self._append(lines, "memory", self._memory(memory))
            for memory_source in session.scalars(select(MemorySourceRow)):
                self._append(
                    lines,
                    "memory_source",
                    {
                        "memory_id": memory_source.memory_id,
                        "source_id": memory_source.source_id,
                    },
                )
            for relation in session.scalars(select(RelationRow)):
                self._append(lines, "relation", self._relation(relation))
            for embedding in session.scalars(select(EmbeddingRow)):
                self._append(lines, "embedding", self._embedding(embedding))
            for entity in session.scalars(select(EntityRow)):
                self._append(lines, "entity", self._entity(entity))
            for identity in session.scalars(select(ClaimIdentityRow)):
                self._append(lines, "claim_identity", self._claim_identity(identity))
            for merge_event in session.scalars(select(EntityMergeEventRow)):
                self._append(lines, "entity_merge", self._entity_merge(merge_event))
            for anchor in session.scalars(select(SourceAnchorRow)):
                self._append(lines, "source_anchor", self._source_anchor(anchor))
            for claim in session.scalars(select(ClaimRow)):
                self._append(lines, "claim", self._claim(claim))
            for version in session.scalars(select(ClaimVersionRow)):
                self._append(lines, "claim_version", self._claim_version(version))
            for evidence in session.scalars(select(ClaimEvidenceRow)):
                self._append(lines, "claim_evidence", self._claim_evidence(evidence))
            for claim_relation in session.scalars(select(ClaimRelationRow)):
                self._append(lines, "claim_relation", self._claim_relation(claim_relation))
            for possible in session.scalars(select(PossibleConflictRow)):
                self._append(lines, "possible_conflict", self._possible_conflict(possible))
            for health in session.scalars(select(MemoryHealthRow)):
                self._append(lines, "memory_health", self._memory_health(health))
            for retrieval_run in session.scalars(select(RetrievalRunRow)):
                self._append(lines, "retrieval_run", self._retrieval_run(retrieval_run))
            for feedback in session.scalars(select(MemoryFeedbackRow)):
                self._append(lines, "feedback", self._feedback(feedback))
            for consolidation in session.scalars(select(ConsolidationCandidateRow)):
                self._append(lines, "consolidation", self._consolidation(consolidation))
            for audit_event in session.scalars(select(AuditEventRow)):
                self._append(lines, "audit", self._audit(audit_event))
            for setting in session.scalars(select(SettingRow)):
                self._append(lines, "setting", {"key": setting.key, "value": setting.value})
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        manifest = {
            "format": "memoryos-jsonl-export",
            "format_version": FORMAT_VERSION,
            "schema_version": self.database.schema_version(),
            "created_at": datetime.now(UTC).isoformat(),
            "data_sha256": _sha256(payload),
            "records": len(lines),
        }
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.writestr("data.jsonl", payload)
        return destination

    def import_jsonl(self, archive_path: Path) -> int:
        archive_path = archive_path.expanduser().resolve()
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if names != {"manifest.json", "data.jsonl"}:
                raise BackupError("import archive contains unexpected or missing entries")
            manifest = json.loads(archive.read("manifest.json"))
            payload = archive.read("data.jsonl")
        if (
            manifest.get("format") != "memoryos-jsonl-export"
            or manifest.get("format_version") not in SUPPORTED_IMPORT_VERSIONS
        ):
            raise BackupError("unsupported import format")
        if not isinstance(manifest.get("data_sha256"), str) or not hmac.compare_digest(
            manifest["data_sha256"], _sha256(payload)
        ):
            raise BackupError("import integrity hash does not match")
        try:
            records = [json.loads(line) for line in payload.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("import contains invalid JSONL") from exc
        allowed = {
            "repository",
            "source",
            "memory",
            "memory_source",
            "relation",
            "embedding",
            "entity",
            "claim_identity",
            "entity_merge",
            "source_anchor",
            "claim",
            "claim_version",
            "claim_evidence",
            "claim_relation",
            "possible_conflict",
            "memory_health",
            "retrieval_run",
            "feedback",
            "consolidation",
            "audit",
            "setting",
        }
        if any(
            not isinstance(record, dict)
            or record.get("type") not in allowed
            or not isinstance(record.get("data"), dict)
            for record in records
        ):
            raise BackupError("import record failed schema validation")

        order = {name: index for index, name in enumerate(allowed)}
        type_order = {
            "repository": 0,
            "source": 1,
            "memory": 2,
            "memory_source": 3,
            "relation": 4,
            "embedding": 5,
            "entity": 6,
            "claim_identity": 7,
            "entity_merge": 8,
            "source_anchor": 9,
            "claim": 10,
            "claim_version": 11,
            "claim_evidence": 12,
            "claim_relation": 13,
            "possible_conflict": 14,
            "memory_health": 15,
            "retrieval_run": 16,
            "feedback": 17,
            "consolidation": 18,
            "audit": 19,
            "setting": 20,
        }
        records.sort(key=lambda record: type_order.get(str(record["type"]), len(order)))
        with self.database.session() as session:
            previous_kind: str | None = None
            for record in records:
                kind = str(record["type"])
                if previous_kind is not None and kind != previous_kind:
                    session.flush()
                data = dict(record["data"])
                row = self._row_from_import(kind, data)
                session.merge(row)
                previous_kind = kind
        return len(records)

    @staticmethod
    def _append(lines: list[str], kind: str, data: dict[str, Any]) -> None:
        lines.append(
            json.dumps({"type": kind, "data": data}, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def _repository(self, row: RepositoryRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "stable_key": row.stable_key,
            "name": row.name,
            "path": row.path,
            "remote_url": row.remote_url,
            "default_branch": row.default_branch,
            "created_at": self._iso(row.created_at),
            "updated_at": self._iso(row.updated_at),
        }

    def _source(self, row: SourceRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "source_type": row.source_type.value,
            "source_ref": row.source_ref,
            "captured_at": self._iso(row.captured_at),
            "excerpt": row.excerpt,
            "content_hash": row.content_hash,
            "metadata_json": row.metadata_json,
            "created_at": self._iso(row.created_at),
        }

    def _memory(self, row: MemoryRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "memory_type": row.memory_type.value,
            "category": row.category,
            "subject": row.subject,
            "key": row.key,
            "title": row.title,
            "content": row.content,
            "status": row.status.value,
            "confidence": row.confidence,
            "importance": row.importance,
            "valid_from": self._iso(row.valid_from),
            "valid_to": self._iso(row.valid_to),
            "ttl_seconds": row.ttl_seconds,
            "supersedes_id": row.supersedes_id,
            "created_at": self._iso(row.created_at),
            "updated_at": self._iso(row.updated_at),
            "created_by": row.created_by.value,
            "sensitivity": row.sensitivity.value,
            "metadata_json": row.metadata_json,
        }

    def _relation(self, row: RelationRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "from_memory_id": row.from_memory_id,
            "to_memory_id": row.to_memory_id,
            "relation_type": row.relation_type,
            "metadata_json": row.metadata_json,
            "created_at": self._iso(row.created_at),
        }

    def _embedding(self, row: EmbeddingRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "memory_id": row.memory_id,
            "provider": row.provider,
            "model": row.model,
            "dimensions": row.dimensions,
            "vector_json": row.vector_json,
            "created_at": self._iso(row.created_at),
        }

    def _entity(self, row: EntityRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "entity_type": row.entity_type.value,
            "canonical_name": row.canonical_name,
            "normalized_name": row.normalized_name,
            "aliases_json": row.aliases_json,
            "stable_external_key": row.stable_external_key,
            "redirect_to_id": row.redirect_to_id,
            "created_at": self._iso(row.created_at),
            "updated_at": self._iso(row.updated_at),
        }

    def _claim_identity(self, row: ClaimIdentityRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "subject_entity_id": row.subject_entity_id,
            "canonical_subject": row.canonical_subject,
            "canonical_predicate": row.canonical_predicate,
            "stable_identity": row.stable_identity,
            "created_at": self._iso(row.created_at),
        }

    def _entity_merge(self, row: EntityMergeEventRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "from_entity_id": row.from_entity_id,
            "to_entity_id": row.to_entity_id,
            "actor": row.actor,
            "rationale": row.rationale,
            "reversible": row.reversible,
            "created_at": self._iso(row.created_at),
        }

    def _source_anchor(self, row: SourceAnchorRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "repository_stable_key": row.repository_stable_key,
            "commit_sha": row.commit_sha,
            "path": row.path,
            "blob_sha": row.blob_sha,
            "language": row.language,
            "symbol_fqn": row.symbol_fqn,
            "symbol_kind": row.symbol_kind,
            "line_start": row.line_start,
            "line_end": row.line_end,
            "evidence_excerpt": row.evidence_excerpt,
            "excerpt_hash": row.excerpt_hash,
            "context_hash": row.context_hash,
            "freshness_state": row.freshness_state.value,
            "cached_head": row.cached_head,
            "checked_at": self._iso(row.checked_at),
            "metadata_json": row.metadata_json,
            "created_at": self._iso(row.created_at),
        }

    def _claim(self, row: ClaimRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "memory_id": row.memory_id,
            "subject_entity_id": row.subject_entity_id,
            "predicate": row.predicate,
            "object_kind": row.object_kind.value,
            "object_entity_id": row.object_entity_id,
            "object_value": row.object_value,
            "polarity": row.polarity.value,
            "modality": row.modality.value,
            "qualifiers_json": row.qualifiers_json,
            "canonical_key": row.canonical_key,
            "confidence": row.confidence,
            "status": row.status.value,
            "valid_from": self._iso(row.valid_from),
            "valid_to": self._iso(row.valid_to),
            "recorded_at": self._iso(row.recorded_at),
            "stale_state": row.stale_state.value,
        }

    def _claim_version(self, row: ClaimVersionRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "claim_id": row.claim_id,
            "identity_id": row.identity_id,
            "memory_id": row.memory_id,
            "version_number": row.version_number,
            "object_kind": row.object_kind.value,
            "object_entity_id": row.object_entity_id,
            "object_value": row.object_value,
            "polarity": row.polarity.value,
            "modality": row.modality.value,
            "qualifiers_json": row.qualifiers_json,
            "valid_from": self._iso(row.valid_from),
            "valid_to": self._iso(row.valid_to),
            "transaction_from": self._iso(row.transaction_from),
            "transaction_to": self._iso(row.transaction_to),
            "status": row.status.value,
            "stale_state": row.stale_state.value,
            "confidence": row.confidence,
            "reason": row.reason,
            "actor": row.actor,
            "source_event_id": row.source_event_id,
            "created_at": self._iso(row.created_at),
        }

    @staticmethod
    def _claim_evidence(row: ClaimEvidenceRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "claim_id": row.claim_id,
            "source_id": row.source_id,
            "evidence_excerpt": row.evidence_excerpt,
            "evidence_hash": row.evidence_hash,
            "source_anchor_id": row.source_anchor_id,
            "support_weight": row.support_weight,
        }

    def _claim_relation(self, row: ClaimRelationRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "from_claim_id": row.from_claim_id,
            "to_claim_id": row.to_claim_id,
            "relation_type": row.relation_type.value,
            "confidence": row.confidence,
            "method": row.method.value,
            "explanation": row.explanation,
            "created_at": self._iso(row.created_at),
        }

    def _possible_conflict(self, row: PossibleConflictRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "left_claim_id": row.left_claim_id,
            "right_claim_id": row.right_claim_id,
            "status": row.status.value,
            "deterministic_relationship": row.deterministic_relationship,
            "deterministic_confidence": row.deterministic_confidence,
            "reason": row.reason,
            "model_result_json": row.model_result_json,
            "provider_fingerprint": row.provider_fingerprint,
            "prompt_version": row.prompt_version,
            "evidence_hash": row.evidence_hash,
            "created_at": self._iso(row.created_at),
            "resolved_at": self._iso(row.resolved_at),
            "resolved_by": row.resolved_by,
        }

    def _memory_health(self, row: MemoryHealthRow) -> dict[str, Any]:
        return {
            "memory_id": row.memory_id,
            "temperature": row.temperature.value,
            "health_score": row.health_score,
            "components_json": row.components_json,
            "explanation": row.explanation,
            "retrieval_count": row.retrieval_count,
            "last_retrieved_at": self._iso(row.last_retrieved_at),
            "archived_at": self._iso(row.archived_at),
            "evaluated_at": self._iso(row.evaluated_at),
        }

    def _retrieval_run(self, row: RetrievalRunRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "query": row.query,
            "task": row.task,
            "scope_json": row.scope_json,
            "selected_memory_ids": row.selected_memory_ids,
            "candidate_features": row.candidate_features,
            "context_manifest": row.context_manifest,
            "config_hash": row.config_hash,
            "created_at": self._iso(row.created_at),
        }

    def _feedback(self, row: MemoryFeedbackRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "retrieval_run_id": row.retrieval_run_id,
            "memory_id": row.memory_id,
            "helpful": row.helpful.value,
            "actor": row.actor,
            "reason": row.reason,
            "created_at": self._iso(row.created_at),
        }

    def _consolidation(self, row: ConsolidationCandidateRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "subject_entity_id": row.subject_entity_id,
            "predicate": row.predicate,
            "proposal_json": row.proposal_json,
            "status": row.status,
            "source_memory_ids": row.source_memory_ids,
            "counterevidence_json": row.counterevidence_json,
            "created_at": self._iso(row.created_at),
        }

    def _audit(self, row: AuditEventRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "actor": row.actor,
            "timestamp": self._iso(row.timestamp),
            "details": row.details,
        }

    @staticmethod
    def _dt(value: Any) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value else None

    def _row_from_import(self, kind: str, data: dict[str, Any]) -> Any:
        if kind == "repository":
            data["created_at"] = self._dt(data.get("created_at"))
            data["updated_at"] = self._dt(data.get("updated_at"))
            return RepositoryRow(**data)
        if kind == "source":
            data["source_type"] = SourceType(data["source_type"])
            data["captured_at"] = self._dt(data.get("captured_at"))
            data["created_at"] = self._dt(data.get("created_at"))
            return SourceRow(**data)
        if kind == "memory":
            data["scope_type"] = ScopeType(data["scope_type"])
            data["memory_type"] = MemoryType(data["memory_type"])
            data["status"] = MemoryStatus(data["status"])
            data["created_by"] = CreatedBy(data["created_by"])
            data["sensitivity"] = Sensitivity(data["sensitivity"])
            for field in ("valid_from", "valid_to", "created_at", "updated_at"):
                data[field] = self._dt(data.get(field))
            return MemoryRow(**data)
        if kind == "memory_source":
            return MemorySourceRow(**data)
        if kind == "relation":
            data["created_at"] = self._dt(data.get("created_at"))
            return RelationRow(**data)
        if kind == "embedding":
            data["created_at"] = self._dt(data.get("created_at"))
            return EmbeddingRow(**data)
        if kind == "entity":
            data["scope_type"] = ScopeType(data["scope_type"])
            data["entity_type"] = EntityType(data["entity_type"])
            data["created_at"] = self._dt(data.get("created_at"))
            data["updated_at"] = self._dt(data.get("updated_at"))
            return EntityRow(**data)
        if kind == "claim_identity":
            data["scope_type"] = ScopeType(data["scope_type"])
            data["created_at"] = self._dt(data.get("created_at"))
            return ClaimIdentityRow(**data)
        if kind == "entity_merge":
            data["created_at"] = self._dt(data.get("created_at"))
            return EntityMergeEventRow(**data)
        if kind == "source_anchor":
            data["freshness_state"] = FreshnessState(data["freshness_state"])
            data["checked_at"] = self._dt(data.get("checked_at"))
            data["created_at"] = self._dt(data.get("created_at"))
            return SourceAnchorRow(**data)
        if kind == "claim":
            data["object_kind"] = ClaimObjectKind(data["object_kind"])
            data["polarity"] = ClaimPolarity(data["polarity"])
            data["modality"] = ClaimModality(data["modality"])
            data["status"] = ClaimStatus(data["status"])
            data["stale_state"] = ClaimStaleState(data["stale_state"])
            for field in ("valid_from", "valid_to", "recorded_at"):
                data[field] = self._dt(data.get(field))
            return ClaimRow(**data)
        if kind == "claim_version":
            data["object_kind"] = ClaimObjectKind(data["object_kind"])
            data["polarity"] = ClaimPolarity(data["polarity"])
            data["modality"] = ClaimModality(data["modality"])
            data["status"] = ClaimStatus(data["status"])
            data["stale_state"] = ClaimStaleState(data["stale_state"])
            for field in (
                "valid_from",
                "valid_to",
                "transaction_from",
                "transaction_to",
                "created_at",
            ):
                data[field] = self._dt(data.get(field))
            return ClaimVersionRow(**data)
        if kind == "claim_evidence":
            return ClaimEvidenceRow(**data)
        if kind == "claim_relation":
            data["relation_type"] = ClaimRelationType(data["relation_type"])
            data["method"] = RelationMethod(data["method"])
            data["created_at"] = self._dt(data.get("created_at"))
            return ClaimRelationRow(**data)
        if kind == "possible_conflict":
            data["status"] = PossibleConflictStatus(data["status"])
            data["created_at"] = self._dt(data.get("created_at"))
            data["resolved_at"] = self._dt(data.get("resolved_at"))
            return PossibleConflictRow(**data)
        if kind == "memory_health":
            data["temperature"] = MemoryTemperature(data["temperature"])
            for field in (
                "last_retrieved_at",
                "archived_at",
                "evaluated_at",
            ):
                data[field] = self._dt(data.get(field))
            return MemoryHealthRow(**data)
        if kind == "retrieval_run":
            data["created_at"] = self._dt(data.get("created_at"))
            return RetrievalRunRow(**data)
        if kind == "feedback":
            data["helpful"] = FeedbackValue(data["helpful"])
            data["created_at"] = self._dt(data.get("created_at"))
            return MemoryFeedbackRow(**data)
        if kind == "consolidation":
            data["scope_type"] = ScopeType(data["scope_type"])
            data["created_at"] = self._dt(data.get("created_at"))
            return ConsolidationCandidateRow(**data)
        if kind == "audit":
            data["timestamp"] = self._dt(data.get("timestamp"))
            return AuditEventRow(**data)
        if kind == "setting":
            return SettingRow(**data)
        raise BackupError(f"unsupported record type: {kind}")
