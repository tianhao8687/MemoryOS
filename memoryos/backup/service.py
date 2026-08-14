from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from memoryos.config import MemoryOSSettings
from memoryos.db.base import Base
from memoryos.db.models import (
    AnnIndexStateRow,
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
from memoryos.integrations.git import sanitize_remote

FORMAT_VERSION = 3
SUPPORTED_IMPORT_VERSIONS = {1, 2, FORMAT_VERSION}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DATABASE_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSONL_IMPORT_BYTES = 512 * 1024 * 1024
MAX_JSONL_RECORD_BYTES = 4 * 1024 * 1024
MAX_IMPORT_RECORDS = 1_000_000
CURRENT_SCHEMA_VERSION = "0004_anchor_observation_hardening"
SUPPORTED_SCHEMA_VERSIONS = {
    "0001_initial",
    "0002_memory_intelligence",
    "0003_reality_intelligence_hardening",
    CURRENT_SCHEMA_VERSION,
}
REQUIRED_FTS_TRIGGERS = {
    "memories_fts_insert",
    "memories_fts_update",
    "memories_fts_delete",
}
IMPORT_TYPE_ORDER = (
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
)
RESTORE_MODELS: tuple[type[Any], ...] = (
    RepositoryRow,
    SourceRow,
    MemoryRow,
    MemorySourceRow,
    RelationRow,
    EmbeddingRow,
    AuditEventRow,
    SettingRow,
    EntityRow,
    ClaimIdentityRow,
    EntityMergeEventRow,
    ClaimRow,
    ClaimVersionRow,
    PossibleConflictRow,
    SourceAnchorRow,
    ClaimEvidenceRow,
    ClaimRelationRow,
    RetrievalRunRow,
    MemoryFeedbackRow,
    ConsolidationCandidateRow,
    AnnIndexStateRow,
    MemoryHealthRow,
)
JSON_FIELD_SHAPES: dict[type[Any], tuple[tuple[str, type[Any] | None], ...]] = {
    SourceRow: (("metadata_json", dict),),
    MemoryRow: (("metadata_json", dict),),
    RelationRow: (("metadata_json", dict),),
    EmbeddingRow: (("vector_json", list),),
    AuditEventRow: (("details", dict),),
    SettingRow: (("value", dict),),
    EntityRow: (("aliases_json", list),),
    ClaimRow: (("object_value", None), ("qualifiers_json", dict)),
    ClaimVersionRow: (("object_value", None), ("qualifiers_json", dict)),
    PossibleConflictRow: (("model_result_json", dict),),
    SourceAnchorRow: (("metadata_json", dict),),
    RetrievalRunRow: (
        ("scope_json", dict),
        ("selected_memory_ids", list),
        ("candidate_features", list),
        ("context_manifest", list),
    ),
    ConsolidationCandidateRow: (
        ("proposal_json", dict),
        ("source_memory_ids", list),
        ("counterevidence_json", list),
    ),
    MemoryHealthRow: (("components_json", dict),),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_nonfinite(item) for item in value)
    return False


def _read_limited(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise BackupError(f"archive entry is missing: {name}") from exc
    if info.is_dir() or info.file_size < 0 or info.file_size > maximum:
        raise BackupError(f"archive entry exceeds the allowed size: {name}")
    with archive.open(info) as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise BackupError(f"archive entry exceeds the allowed size: {name}")
    return payload


def _extract_limited(
    archive: zipfile.ZipFile,
    name: str,
    destination: Path,
    maximum: int,
) -> str:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise BackupError(f"archive entry is missing: {name}") from exc
    if info.is_dir() or info.file_size < 0 or info.file_size > maximum:
        raise BackupError(f"archive entry exceeds the allowed size: {name}")
    digest = hashlib.sha256()
    written = 0
    with archive.open(info) as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > maximum:
                raise BackupError(f"archive entry exceeds the allowed size: {name}")
            digest.update(chunk)
            target.write(chunk)
    if written != info.file_size:
        raise BackupError(f"archive entry size does not match its ZIP metadata: {name}")
    return digest.hexdigest()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            while raw_line := handle.readline(MAX_JSONL_RECORD_BYTES + 1):
                if len(raw_line) > MAX_JSONL_RECORD_BYTES:
                    raise BackupError("import contains an oversized JSONL record")
                if not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise BackupError("import contains invalid JSONL") from exc
                if not isinstance(record, dict):
                    raise BackupError("import record failed schema validation")
                yield record
    except OSError as exc:
        raise BackupError("staged import data is unreadable") from exc


class _JsonlArchiveWriter:
    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.count = 0
        self._digest = hashlib.sha256()

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def append(self, value: str) -> None:
        payload = (value + "\n").encode("utf-8")
        if len(payload) > MAX_JSONL_RECORD_BYTES:
            raise BackupError("export contains an oversized JSONL record")
        self.handle.write(payload)
        self._digest.update(payload)
        self.count += 1


def _open_import_archive(path: Path, expected: set[str]) -> zipfile.ZipFile:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupError("archive exceeds the allowed compressed size")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError("archive is corrupt or unreadable") from exc
    names = archive.namelist()
    if len(names) != len(expected) or set(names) != expected:
        archive.close()
        raise BackupError("archive contains unexpected, duplicate, or missing entries")
    return archive


def _normalized_sql(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _schema_signature(database: Database) -> dict[str, Any]:
    inspector = inspect(database.engine)
    tables: dict[str, Any] = {}
    for table_name in sorted(Base.metadata.tables):
        columns = tuple(
            (
                str(column["name"]),
                str(column["type"]).upper(),
                bool(column["nullable"]),
                _normalized_sql(column.get("default")),
            )
            for column in inspector.get_columns(table_name)
        )
        primary_key = tuple(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        )
        foreign_keys = tuple(
            sorted(
                (
                    tuple(key.get("constrained_columns") or []),
                    str(key.get("referred_table") or ""),
                    tuple(key.get("referred_columns") or []),
                    str((key.get("options") or {}).get("ondelete") or "").upper(),
                    str((key.get("options") or {}).get("onupdate") or "").upper(),
                )
                for key in inspector.get_foreign_keys(table_name)
            )
        )
        unique_constraints = tuple(
            sorted(
                tuple(constraint.get("column_names") or [])
                for constraint in inspector.get_unique_constraints(table_name)
            )
        )
        indexes = tuple(
            sorted(
                (
                    str(index.get("name") or ""),
                    tuple(index.get("column_names") or []),
                    bool(index.get("unique")),
                )
                for index in inspector.get_indexes(table_name)
            )
        )
        checks = tuple(
            sorted(
                (
                    str(constraint.get("name") or ""),
                    _normalized_sql(constraint.get("sqltext")),
                )
                for constraint in inspector.get_check_constraints(table_name)
            )
        )
        tables[table_name] = {
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": foreign_keys,
            "unique_constraints": unique_constraints,
            "indexes": indexes,
            "checks": checks,
        }
    with database.engine.connect() as connection:
        schema_objects = tuple(
            sorted(
                (
                    str(row.type),
                    str(row.name),
                    str(row.tbl_name),
                    _normalized_sql(row.sql),
                )
                for row in connection.execute(
                    text(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%'"
                    )
                )
            )
        )
    return {"tables": tables, "schema_objects": schema_objects}


def _utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bounded_number(value: Any, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def _valid_interval(start: datetime | None, end: datetime | None) -> bool:
    return start is None or end is None or _utc_datetime(start) < _utc_datetime(end)


def _validate_runtime_data(database: Database) -> None:
    try:
        with database.session() as session:
            for model in RESTORE_MODELS:
                rows = session.scalars(select(model).execution_options(yield_per=500))
                for row in rows:
                    for field, expected_type in JSON_FIELD_SHAPES.get(model, ()):
                        value = getattr(row, field)
                        if (
                            value is not None
                            and expected_type is not None
                            and not isinstance(value, expected_type)
                        ):
                            raise BackupError(
                                "backup database has an invalid JSON shape in "
                                f"{model.__tablename__}"
                            )
                        if _contains_nonfinite(value):
                            raise BackupError(
                                f"backup database has non-finite JSON in {model.__tablename__}"
                            )
                    if isinstance(row, MemoryRow):
                        if (
                            not _bounded_number(row.confidence, 0.0, 1.0)
                            or not _bounded_number(row.importance, 0.0, 1.0)
                            or not _valid_interval(row.valid_from, row.valid_to)
                            or (
                                row.ttl_seconds is not None
                                and (
                                    isinstance(row.ttl_seconds, bool)
                                    or not 0 < row.ttl_seconds <= 315_360_000
                                )
                            )
                        ):
                            raise BackupError("backup database has an invalid memory record")
                    elif isinstance(row, EmbeddingRow):
                        vector = row.vector_json
                        if (
                            isinstance(row.dimensions, bool)
                            or not 1 <= row.dimensions <= 65_536
                            or (
                                vector is not None
                                and (
                                    len(vector) != row.dimensions
                                    or any(
                                        not isinstance(value, (int, float))
                                        or isinstance(value, bool)
                                        or not math.isfinite(float(value))
                                        for value in vector
                                    )
                                )
                            )
                            or (
                                row.vector_blob is not None
                                and len(row.vector_blob) != row.dimensions * 4
                            )
                        ):
                            raise BackupError("backup database has an invalid embedding record")
                    elif isinstance(row, ClaimRow):
                        if not _bounded_number(row.confidence, 0.0, 1.0) or not _valid_interval(
                            row.valid_from, row.valid_to
                        ):
                            raise BackupError("backup database has an invalid claim record")
                    elif isinstance(row, ClaimVersionRow):
                        if (
                            row.version_number < 1
                            or not _bounded_number(row.confidence, 0.0, 1.0)
                            or not _valid_interval(row.valid_from, row.valid_to)
                            or not _valid_interval(row.transaction_from, row.transaction_to)
                        ):
                            raise BackupError("backup database has an invalid claim version")
                    elif isinstance(row, PossibleConflictRow):
                        if not _bounded_number(row.deterministic_confidence, 0.0, 1.0):
                            raise BackupError("backup database has an invalid possible conflict")
                    elif isinstance(row, SourceAnchorRow):
                        if (
                            (row.line_start is not None and row.line_start < 1)
                            or (row.line_end is not None and row.line_end < 1)
                            or (
                                row.line_start is not None
                                and row.line_end is not None
                                and row.line_end < row.line_start
                            )
                        ):
                            raise BackupError("backup database has an invalid source anchor")
                    elif isinstance(row, ClaimEvidenceRow):
                        if not _bounded_number(row.support_weight, 0.0, 1.0):
                            raise BackupError("backup database has invalid claim evidence")
                    elif isinstance(row, ClaimRelationRow):
                        if not _bounded_number(row.confidence, 0.0, 1.0):
                            raise BackupError("backup database has an invalid claim relation")
                    elif isinstance(row, AnnIndexStateRow):
                        if row.dimensions < 1 or row.item_count < 0:
                            raise BackupError("backup database has invalid ANN state")
                    elif isinstance(row, MemoryHealthRow) and (
                        not _bounded_number(row.health_score, 0.0, 1.0) or row.retrieval_count < 0
                    ):
                        raise BackupError("backup database has invalid memory health")
                    session.expunge(row)
    except BackupError:
        raise
    except (
        json.JSONDecodeError,
        OverflowError,
        SQLAlchemyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise BackupError("backup database row data is malformed") from exc


def _validate_runtime_schema(database: Database) -> None:
    inspector = inspect(database.engine)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    missing_tables = expected_tables - actual_tables
    if missing_tables:
        raise BackupError(
            f"backup database schema is missing tables: {', '.join(sorted(missing_tables))}"
        )
    for table_name, table in Base.metadata.tables.items():
        actual_columns = {str(column["name"]) for column in inspector.get_columns(table_name)}
        missing_columns = {column.name for column in table.columns} - actual_columns
        if missing_columns:
            raise BackupError(
                "backup database schema is missing columns in "
                f"{table_name}: {', '.join(sorted(missing_columns))}"
            )
    with database.engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != CURRENT_SCHEMA_VERSION:
            raise BackupError("backup database schema did not migrate to the current version")
        foreign_key_error = connection.execute(text("PRAGMA foreign_key_check")).first()
        if foreign_key_error is not None:
            raise BackupError("backup database failed foreign-key validation")
        sqlite_objects = {
            (str(row.type), str(row.name))
            for row in connection.execute(
                text("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')")
            )
        }
    if ("table", "memory_fts") not in sqlite_objects or any(
        ("trigger", trigger) not in sqlite_objects for trigger in REQUIRED_FTS_TRIGGERS
    ):
        raise BackupError("backup database schema is missing the FTS table or triggers")
    with tempfile.TemporaryDirectory(
        prefix="memoryos-schema-reference-", dir=database.settings.data_dir
    ) as directory:
        reference_settings = database.settings.model_copy(update={"data_dir": Path(directory)})
        reference = Database(reference_settings)
        try:
            reference.initialize()
            if _schema_signature(database) != _schema_signature(reference):
                raise BackupError("backup database schema differs from the current schema")
        finally:
            reference.close()
    _validate_runtime_data(database)


class BackupService:
    def __init__(self, database: Database, settings: MemoryOSSettings) -> None:
        self.database = database
        self.settings = settings

    def create_backup(self, destination: Path | None = None) -> Path:
        if destination is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            destination = self.settings.backup_dir / f"memoryos-{stamp}.zip"
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="memoryos-backup-stage-", dir=self.settings.data_dir
        ) as directory:
            staged_database = Path(directory) / "memoryos.db"
            source = sqlite3.connect(self.settings.database_path)
            target = sqlite3.connect(staged_database)
            try:
                source.backup(target)
                target.commit()
                schema_version = str(
                    target.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                )
            except (OSError, sqlite3.DatabaseError, TypeError) as exc:
                raise BackupError("live database could not be snapshotted") from exc
            finally:
                target.close()
                source.close()
            manifest = {
                "format": "memoryos-sqlite-backup",
                "format_version": FORMAT_VERSION,
                "schema_version": schema_version,
                "created_at": datetime.now(UTC).isoformat(),
                "database_sha256": _file_sha256(staged_database),
            }
            with tempfile.NamedTemporaryFile(
                suffix=".zip", dir=destination.parent, delete=False
            ) as temporary:
                staged_archive = Path(temporary.name)
            try:
                with zipfile.ZipFile(
                    staged_archive, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2))
                    archive.write(staged_database, "memoryos.db")
                os.replace(staged_archive, destination)
            finally:
                staged_archive.unlink(missing_ok=True)
        return destination

    def restore(self, archive_path: Path, *, create_safety_backup: bool = True) -> Path | None:
        archive_path = archive_path.expanduser().resolve()
        if not archive_path.is_file():
            raise BackupError(f"backup archive does not exist: {archive_path}")
        with tempfile.TemporaryDirectory(
            prefix="memoryos-restore-", dir=self.settings.data_dir
        ) as directory:
            staging_settings = self.settings.model_copy(update={"data_dir": Path(directory)})
            staging_path = staging_settings.database_path
            try:
                with _open_import_archive(
                    archive_path, {"manifest.json", "memoryos.db"}
                ) as archive:
                    manifest = json.loads(
                        _read_limited(archive, "manifest.json", MAX_MANIFEST_BYTES)
                    )
                    if not isinstance(manifest, dict):
                        raise BackupError("backup manifest must be a JSON object")
                    format_version = manifest.get("format_version")
                    if (
                        manifest.get("format") != "memoryos-sqlite-backup"
                        or not isinstance(format_version, int)
                        or isinstance(format_version, bool)
                        or format_version not in SUPPORTED_IMPORT_VERSIONS
                    ):
                        raise BackupError("unsupported backup format")
                    manifest_schema = manifest.get("schema_version")
                    if (
                        not isinstance(manifest_schema, str)
                        or manifest_schema not in SUPPORTED_SCHEMA_VERSIONS
                    ):
                        raise BackupError("unsupported backup database schema version")
                    database_hash = _extract_limited(
                        archive,
                        "memoryos.db",
                        staging_path,
                        MAX_DATABASE_IMPORT_BYTES,
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                raise BackupError("backup manifest or archive is invalid") from exc
            if not isinstance(manifest.get("database_sha256"), str) or not hmac.compare_digest(
                manifest["database_sha256"], database_hash
            ):
                raise BackupError("backup integrity hash does not match")
            try:
                source = sqlite3.connect(staging_path)
                try:
                    integrity = source.execute("PRAGMA integrity_check").fetchone()
                    if not integrity or integrity[0] != "ok":
                        raise BackupError("restored database failed integrity_check")
                    embedded_schema = source.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()
                    if not embedded_schema or embedded_schema[0] != manifest_schema:
                        raise BackupError("backup manifest schema does not match the database")
                finally:
                    source.close()
                staging_database = Database(staging_settings)
                try:
                    staging_database.initialize()
                    _validate_runtime_schema(staging_database)
                    with staging_database.session() as session:
                        session.execute(delete(AnnIndexStateRow))
                    staging_database.checkpoint()
                finally:
                    staging_database.close()
            except BackupError:
                raise
            except (OSError, sqlite3.DatabaseError, SQLAlchemyError, ValueError) as exc:
                raise BackupError("backup database schema is corrupt or unreadable") from exc

            safety = self.create_backup() if create_safety_backup else None
            self._invalidate_ann_cache()
            self.database.checkpoint()
            self.database.close()
            rollback_path: Path | None = None
            replaced = False
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".rollback", dir=self.settings.data_dir, delete=False
                ) as rollback:
                    rollback_path = Path(rollback.name)
                shutil.copy2(self.settings.database_path, rollback_path)
                self._remove_database_sidecars()
                os.replace(staging_path, self.settings.database_path)
                replaced = True
                self.database.initialize()
                return safety
            except Exception as exc:
                self.database.close()
                if replaced and rollback_path is not None and rollback_path.is_file():
                    self._remove_database_sidecars()
                    os.replace(rollback_path, self.settings.database_path)
                try:
                    self.database.initialize()
                except Exception as rollback_exc:
                    raise BackupError(
                        "restore and automatic database rollback both failed"
                    ) from rollback_exc
                raise BackupError("restore failed; the live database was rolled back") from exc
            finally:
                if rollback_path is not None:
                    rollback_path.unlink(missing_ok=True)

    def export_jsonl(self, destination: Path) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".zip", dir=destination.parent, delete=False
        ) as temporary:
            staged_archive = Path(temporary.name)
        try:
            with zipfile.ZipFile(
                staged_archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                with archive.open("data.jsonl", "w", force_zip64=True) as payload_handle:
                    writer = _JsonlArchiveWriter(payload_handle)
                    self._write_jsonl_records(writer)
                manifest = {
                    "format": "memoryos-jsonl-export",
                    "format_version": FORMAT_VERSION,
                    "schema_version": self.database.schema_version(),
                    "created_at": datetime.now(UTC).isoformat(),
                    "data_sha256": writer.sha256,
                    "records": writer.count,
                }
                archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            os.replace(staged_archive, destination)
        finally:
            staged_archive.unlink(missing_ok=True)
        return destination

    def _write_jsonl_records(self, lines: _JsonlArchiveWriter) -> None:
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

    def import_jsonl(self, archive_path: Path) -> int:
        archive_path = archive_path.expanduser().resolve()
        if not archive_path.is_file():
            raise BackupError(f"import archive does not exist: {archive_path}")
        with tempfile.TemporaryDirectory(
            prefix="memoryos-jsonl-import-", dir=self.settings.data_dir
        ) as directory:
            staging_dir = Path(directory)
            payload_path = staging_dir / "data.jsonl"
            try:
                with _open_import_archive(archive_path, {"manifest.json", "data.jsonl"}) as archive:
                    manifest = json.loads(
                        _read_limited(archive, "manifest.json", MAX_MANIFEST_BYTES)
                    )
                    if not isinstance(manifest, dict):
                        raise BackupError("import manifest must be a JSON object")
                    format_version = manifest.get("format_version")
                    if (
                        manifest.get("format") != "memoryos-jsonl-export"
                        or not isinstance(format_version, int)
                        or isinstance(format_version, bool)
                        or format_version not in SUPPORTED_IMPORT_VERSIONS
                    ):
                        raise BackupError("unsupported import format")
                    payload_hash = _extract_limited(
                        archive,
                        "data.jsonl",
                        payload_path,
                        MAX_JSONL_IMPORT_BYTES,
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                raise BackupError("import manifest or archive is invalid") from exc
            if not isinstance(manifest.get("data_sha256"), str) or not hmac.compare_digest(
                manifest["data_sha256"], payload_hash
            ):
                raise BackupError("import integrity hash does not match")
            declared_records = manifest.get("records")
            if declared_records is not None and (
                not isinstance(declared_records, int)
                or isinstance(declared_records, bool)
                or not 0 <= declared_records <= MAX_IMPORT_RECORDS
            ):
                raise BackupError("import manifest has an invalid record count")

            allowed = set(IMPORT_TYPE_ORDER)
            type_paths = {
                kind: staging_dir / f"{index:02d}-{kind}.jsonl"
                for index, kind in enumerate(IMPORT_TYPE_ORDER)
            }
            count = 0
            try:
                with ExitStack() as stack:
                    writers = {
                        kind: stack.enter_context(path.open("wb"))
                        for kind, path in type_paths.items()
                    }
                    for record in _iter_jsonl(payload_path):
                        if _contains_nonfinite(record):
                            raise BackupError("import record contains a non-finite number")
                        kind = record.get("type")
                        if (
                            not isinstance(kind, str)
                            or kind not in allowed
                            or not isinstance(record.get("data"), dict)
                        ):
                            raise BackupError("import record failed schema validation")
                        count += 1
                        if count > MAX_IMPORT_RECORDS:
                            raise BackupError("import contains too many records")
                        normalized = (
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                            + b"\n"
                        )
                        if len(normalized) > MAX_JSONL_RECORD_BYTES:
                            raise BackupError("import contains an oversized JSONL record")
                        writers[str(kind)].write(normalized)
            except (OSError, RecursionError) as exc:
                raise BackupError("import staging failed") from exc
            if declared_records is not None and declared_records != count:
                raise BackupError("import record count does not match the manifest")
            payload_path.unlink(missing_ok=True)

            self._invalidate_ann_cache()
            try:
                with self.database.session() as session:
                    session.execute(delete(AnnIndexStateRow))
                    for kind in IMPORT_TYPE_ORDER:
                        for record in _iter_jsonl(type_paths[kind]):
                            data = dict(record["data"])
                            row = self._row_from_import(kind, data)
                            session.merge(row)
                        session.flush()
            except BackupError:
                raise
            except (KeyError, TypeError, ValueError, SQLAlchemyError) as exc:
                raise BackupError("import record failed schema or integrity validation") from exc
            return count

    def _invalidate_ann_cache(self) -> None:
        paths: set[Path] = set()
        for pattern in ("*.sqlite", "*.sqlite-wal", "*.sqlite-shm"):
            paths.update(self.settings.ann_dir.glob(pattern))
        try:
            for path in paths:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise BackupError("persistent ANN cache could not be invalidated") from exc

    def _remove_database_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            Path(f"{self.settings.database_path}{suffix}").unlink(missing_ok=True)

    @staticmethod
    def _append(lines: _JsonlArchiveWriter, kind: str, data: dict[str, Any]) -> None:
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
            "remote_url": sanitize_remote(row.remote_url) if row.remote_url else None,
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
            "observed_head": row.observed_head,
            "observed_path": row.observed_path,
            "observed_line_start": row.observed_line_start,
            "observed_line_end": row.observed_line_end,
            "observed_excerpt_hash": row.observed_excerpt_hash,
            "observed_at": self._iso(row.observed_at),
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
            if isinstance(data.get("remote_url"), str):
                data["remote_url"] = sanitize_remote(data["remote_url"])
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
            if (
                data["valid_from"] is not None
                and data["valid_to"] is not None
                and data["valid_to"] <= data["valid_from"]
            ):
                raise BackupError("imported memory has an invalid validity interval")
            if not 0 <= float(data["confidence"]) <= 1 or not 0 <= float(data["importance"]) <= 1:
                raise BackupError("imported memory has an invalid score")
            ttl = data.get("ttl_seconds")
            if ttl is not None and (
                not isinstance(ttl, int) or isinstance(ttl, bool) or not 0 < ttl <= 315_360_000
            ):
                raise BackupError("imported memory has an invalid TTL")
            return MemoryRow(**data)
        if kind == "memory_source":
            return MemorySourceRow(**data)
        if kind == "relation":
            data["created_at"] = self._dt(data.get("created_at"))
            return RelationRow(**data)
        if kind == "embedding":
            dimensions = data.get("dimensions")
            vector = data.get("vector_json")
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or not 1 <= dimensions <= 65_536
                or not isinstance(vector, list)
                or len(vector) != dimensions
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    for value in vector
                )
            ):
                raise BackupError("imported embedding vector is invalid")
            data["vector_json"] = [float(value) for value in vector]
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
            data["observed_at"] = self._dt(data.get("observed_at"))
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
