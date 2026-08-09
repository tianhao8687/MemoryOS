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
    EmbeddingRow,
    MemoryRow,
    MemorySourceRow,
    RelationRow,
    RepositoryRow,
    SettingRow,
    SourceRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    CreatedBy,
    MemoryStatus,
    MemoryType,
    ScopeType,
    Sensitivity,
    SourceType,
)
from memoryos.errors import BackupError

FORMAT_VERSION = 1


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
            or manifest.get("format_version") != FORMAT_VERSION
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
            or manifest.get("format_version") != FORMAT_VERSION
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
            "audit": 6,
            "setting": 7,
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
        if kind == "audit":
            data["timestamp"] = self._dt(data.get("timestamp"))
            return AuditEventRow(**data)
        if kind == "setting":
            return SettingRow(**data)
        raise BackupError(f"unsupported record type: {kind}")
