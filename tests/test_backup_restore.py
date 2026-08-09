from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from memoryos.backup import BackupService
from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.domain.schemas import SearchRequest
from memoryos.engine import MemoryService
from memoryos.errors import BackupError


def test_backup_restore_round_trip(
    database: Database, service: MemoryService, settings: Any, make_memory: Any, tmp_path: Path
) -> None:
    memory = service.propose(make_memory(), actor="test")
    backups = BackupService(database, settings)
    archive = backups.create_backup(tmp_path / "backup.zip")
    service.forget(memory["id"], actor="test")
    assert service.search(SearchRequest(query="FastAPI"))["total"] == 0
    safety = backups.restore(archive)
    assert safety is not None and safety.exists()
    assert service.search(SearchRequest(query="FastAPI"))["total"] == 1
    assert database.integrity_check() == "ok"


def test_jsonl_export_import_is_versioned_and_validated(
    database: Database, service: MemoryService, settings: Any, make_memory: Any, tmp_path: Path
) -> None:
    service.propose(make_memory(), actor="test")
    export_path = BackupService(database, settings).export_jsonl(tmp_path / "export.zip")

    imported_settings = settings_for(tmp_path / "imported-data")
    imported_database = Database(imported_settings)
    imported_database.initialize()
    try:
        count = BackupService(imported_database, imported_settings).import_jsonl(export_path)
        imported_service = MemoryService(imported_database, imported_settings)
        assert count >= 4
        assert imported_service.search(SearchRequest(query="FastAPI"))["total"] == 1
        assert imported_database.integrity_check() == "ok"
    finally:
        imported_database.close()


def test_corrupt_database_backup_is_rejected_without_replacing_live_data(
    database: Database,
    service: MemoryService,
    settings: Any,
    make_memory: Any,
    tmp_path: Path,
) -> None:
    memory = service.propose(make_memory(), actor="test")
    backup = BackupService(database, settings)
    valid_archive = backup.create_backup(tmp_path / "valid.zip")
    with zipfile.ZipFile(valid_archive) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    corrupt_bytes = b"not-a-sqlite-database"
    manifest["database_sha256"] = hashlib.sha256(corrupt_bytes).hexdigest()
    corrupt_archive = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memoryos.db", corrupt_bytes)

    with pytest.raises(BackupError):
        backup.restore(corrupt_archive)
    assert service.get(memory["id"])["status"] == "active"
    assert database.integrity_check() == "ok"
