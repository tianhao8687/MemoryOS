from __future__ import annotations

import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select

from memoryos.backup import BackupService
from memoryos.config import settings_for
from memoryos.context.delta import ContextSnapshotStore, scope_fingerprint
from memoryos.context.renderers import render_full
from memoryos.context.token_meter import FunctionTokenCounter, UnicodeHeuristicTokenCounter
from memoryos.db import Database
from memoryos.db.models import ContextSnapshotRow
from memoryos.domain.schemas import ContextRequest
from memoryos.engine import MemoryService


def _migration_config(database: Database) -> Config:
    migrations = Path(__file__).resolve().parents[1] / "memoryos" / "db" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    return config


@pytest.mark.v23
def test_context_efficiency_migration_upgrades_and_downgrades_sqlite(database: Database) -> None:
    with database.engine.begin() as connection:
        config = _migration_config(database)
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_anchor_observation_hardening")
    downgraded = inspect(database.engine)
    assert "context_snapshots" not in downgraded.get_table_names()
    assert "context_usage_json" not in {
        column["name"] for column in downgraded.get_columns("retrieval_runs")
    }

    with database.engine.begin() as connection:
        config = _migration_config(database)
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    upgraded = inspect(database.engine)
    assert "context_snapshots" in upgraded.get_table_names()
    retrieval_columns = {column["name"] for column in upgraded.get_columns("retrieval_runs")}
    assert {
        "context_usage_json",
        "context_policy_manifest",
        "context_diagnostics_json",
        "context_shadow_json",
    }.issubset(retrieval_columns)


@pytest.mark.v23
def test_snapshot_cleanup_is_scope_bound_and_batch_limited(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path / "cleanup-data",
        context_snapshot_cleanup_batch_size=2,
    )
    database = Database(settings)
    database.initialize()
    request_a = ContextRequest(task="a", repository="repo-a")
    request_b = ContextRequest(task="b", repository="repo-b")
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            for index in range(3):
                session.add(
                    ContextSnapshotRow(
                        id=f"scope-a-{index}",
                        request_fingerprint="a" * 64,
                        scope_fingerprint=scope_fingerprint(request_a),
                        policy_hash="b" * 64,
                        tokenizer_id="fixture",
                        counter_kind="estimated",
                        items_json=[],
                        full_text_sha256="c" * 64,
                        full_estimated_tokens=0,
                        created_at=now - timedelta(days=2),
                        expires_at=now - timedelta(days=1),
                    )
                )
            session.add(
                ContextSnapshotRow(
                    id="scope-b-0",
                    request_fingerprint="a" * 64,
                    scope_fingerprint=scope_fingerprint(request_b),
                    policy_hash="b" * 64,
                    tokenizer_id="fixture",
                    counter_kind="estimated",
                    items_json=[],
                    full_text_sha256="c" * 64,
                    full_estimated_tokens=0,
                    created_at=now - timedelta(days=2),
                    expires_at=now - timedelta(days=1),
                )
            )

        deleted = ContextSnapshotStore(database, settings).cleanup_expired(
            scope_fingerprint(request_a),
            now=now,
        )

        assert deleted == 2
        with database.session() as session:
            remaining_a = int(
                session.scalar(
                    select(func.count())
                    .select_from(ContextSnapshotRow)
                    .where(ContextSnapshotRow.scope_fingerprint == scope_fingerprint(request_a))
                )
                or 0
            )
            remaining_b = int(
                session.scalar(
                    select(func.count())
                    .select_from(ContextSnapshotRow)
                    .where(ContextSnapshotRow.scope_fingerprint == scope_fingerprint(request_b))
                )
                or 0
            )
        assert remaining_a == 1
        assert remaining_b == 1
    finally:
        database.close()


@pytest.mark.v23
def test_snapshot_lookup_distinguishes_expiry_tokenizer_policy_and_integrity(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path / "lookup-data")
    database = Database(settings)
    database.initialize()
    store = ContextSnapshotStore(database, settings)
    request = ContextRequest(task="lookup", repository="repo-a")
    estimated = UnicodeHeuristicTokenCounter()
    exact = FunctionTokenCounter(
        tokenizer_id="fixture-exact-v1",
        counter_version="1",
        count=len,
    )
    policy_hash = "b" * 64
    now = datetime.now(UTC)
    full_text = render_full([])
    try:
        for context_id in ("expired", "tokenizer", "policy", "integrity"):
            store.create(
                context_id=context_id,
                base_snapshot_id=None,
                request=request,
                policy_hash=policy_hash,
                counter=estimated,
                atoms=[],
                full_text=full_text,
                full_tokens=estimated.count_text(full_text),
                now=now,
            )
        with database.session() as session:
            expired = session.get(ContextSnapshotRow, "expired")
            integrity = session.get(ContextSnapshotRow, "integrity")
            assert expired is not None and integrity is not None
            expired.expires_at = now - timedelta(seconds=1)
            integrity.full_text_sha256 = "0" * 64

        expired_lookup = store.load_valid(
            "expired",
            request,
            policy_hash=policy_hash,
            counter=estimated,
            now=now,
        )
        tokenizer_lookup = store.load_valid(
            "tokenizer",
            request,
            policy_hash=policy_hash,
            counter=exact,
            now=now,
        )
        policy_lookup = store.load_valid(
            "policy",
            request,
            policy_hash="c" * 64,
            counter=estimated,
            now=now,
        )
        integrity_lookup = store.load_valid(
            "integrity",
            request,
            policy_hash=policy_hash,
            counter=estimated,
            now=now,
        )

        assert expired_lookup.fallback_reason == "snapshot_expired"
        assert tokenizer_lookup.fallback_reason == "tokenizer_mismatch"
        assert policy_lookup.fallback_reason == "policy_mismatch"
        assert integrity_lookup.fallback_reason == "snapshot_integrity_failure"
    finally:
        database.close()


@pytest.mark.v23
def test_backup_excludes_snapshot_cache_and_restore_full_rebases(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    settings = settings_for(tmp_path / "backup-data", context_compiler_mode="msc")
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    backups = BackupService(database, settings)
    try:
        service.propose(make_memory(), actor="test")
        first = service.context(
            ContextRequest(task="FastAPI", repository="repo-a", budget_tokens=5000)
        )
        archive_path = backups.create_backup(tmp_path / "snapshot-free.zip")
        extracted = tmp_path / "archived-memoryos.db"
        with zipfile.ZipFile(archive_path) as archive:
            extracted.write_bytes(archive.read("memoryos.db"))
        archived = sqlite3.connect(extracted)
        try:
            assert archived.execute("SELECT count(*) FROM context_snapshots").fetchone()[0] == 0
        finally:
            archived.close()

        backups.restore(archive_path, create_safety_backup=False)
        restored = MemoryService(database, settings)
        rebased = restored.context(
            ContextRequest(
                task="FastAPI",
                repository="repo-a",
                budget_tokens=5000,
                previous_context_id=first["context_id"],
                response_mode="delta",
            )
        )

        assert rebased["mode"] == "full"
        assert rebased["fallback_reason"] == "snapshot_unavailable"
        assert "fastapi" in rebased["text"].lower()
    finally:
        database.close()


@pytest.mark.v23
def test_jsonl_import_invalidates_existing_snapshot_cache(
    tmp_path: Path,
    make_memory: Any,
) -> None:
    source_settings = settings_for(tmp_path / "jsonl-source")
    source_database = Database(source_settings)
    source_database.initialize()
    target_settings = settings_for(tmp_path / "jsonl-target", context_compiler_mode="msc")
    target_database = Database(target_settings)
    target_database.initialize()
    now = datetime.now(UTC)
    try:
        MemoryService(source_database, source_settings).propose(make_memory(), actor="test")
        archive = BackupService(source_database, source_settings).export_jsonl(
            tmp_path / "truth-import.zip"
        )
        with target_database.session() as session:
            session.add(
                ContextSnapshotRow(
                    id="pre-import-snapshot",
                    base_snapshot_id=None,
                    request_fingerprint="a" * 64,
                    scope_fingerprint="b" * 64,
                    policy_hash="c" * 64,
                    tokenizer_id="unicode-heuristic-v1",
                    counter_kind="estimated",
                    items_json=[],
                    full_text_sha256="d" * 64,
                    full_estimated_tokens=1,
                    created_at=now,
                    expires_at=now + timedelta(days=7),
                )
            )

        BackupService(target_database, target_settings).import_jsonl(archive)

        with target_database.session() as session:
            assert session.scalar(select(func.count()).select_from(ContextSnapshotRow)) == 0
    finally:
        target_database.close()
        source_database.close()
