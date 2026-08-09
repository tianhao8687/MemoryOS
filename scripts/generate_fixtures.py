from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import func, select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import MemoryRow, RepositoryRow
from memoryos.domain.schemas import (
    CreatedBy,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService

ACTIVE_FIXTURES = [
    (
        "Use SQLite WAL for local storage",
        "SQLite uses WAL mode for resilient local concurrency.",
        "decision",
        "storage.sqlite.mode",
        MemoryType.PROJECT,
    ),
    (
        "Backend framework: FastAPI",
        "FastAPI and Pydantic v2 are the shared local API foundation.",
        "decision",
        "architecture.backend.framework",
        MemoryType.PROJECT,
    ),
    (
        "Keep all timestamps in UTC",
        "Persist timezone-aware UTC and localize only in the management UI.",
        "decision",
        "data.timestamps",
        MemoryType.PROJECT,
    ),
    (
        "No Redis in V1",
        "Do not introduce Redis, Kafka, or another distributed dependency in V1.",
        "constraint",
        "infrastructure.cache",
        MemoryType.PROCEDURAL,
    ),
    (
        "Localhost binding only",
        "The HTTP service must bind 127.0.0.1 by default.",
        "constraint",
        "security.http.binding",
        MemoryType.PROCEDURAL,
    ),
    (
        "Every active memory has provenance",
        "Confirmed memory must retain at least one hashed source.",
        "constraint",
        "data.provenance.required",
        MemoryType.PROCEDURAL,
    ),
    (
        "FTS corruption on abrupt exit",
        "An uncheckpointed prototype corrupted its hand-written FTS mirror; "
        "use transactional triggers.",
        "failure",
        "failure.fts.abrupt-exit",
        MemoryType.EPISODIC,
    ),
    (
        "Large write transactions block readers",
        "Avoid write transactions longer than ten seconds; chunk fixture imports.",
        "failure",
        "failure.sqlite.long-write",
        MemoryType.EPISODIC,
    ),
    (
        "Windows path case mismatch",
        "Normalize resolved Windows paths before comparing local repository locations.",
        "failure",
        "failure.windows.path-case",
        MemoryType.EPISODIC,
    ),
    (
        "Prefer offline-first operation",
        "Core memory capture and retrieval must work without a provider or API key.",
        "preference",
        "preference.operation.offline",
        MemoryType.PREFERENCE,
    ),
    (
        "Current release target",
        "Finish V1 verification, Windows packaging, and production smoke.",
        "state",
        "release.v1.current",
        MemoryType.WORKING,
    ),
]

CANDIDATE_FIXTURES = [
    (
        "Admin endpoints require auth",
        "All write endpoints require the local bearer token and a trusted Origin.",
        "constraint",
        "security.admin.auth",
        MemoryType.PROCEDURAL,
    ),
    (
        "Backend framework: Django",
        "Replace FastAPI with Django for the backend framework.",
        "decision",
        "architecture.backend.framework",
        MemoryType.PROJECT,
    ),
    (
        "Introduce Redis for caching",
        "Add Redis to cache context responses in V1.",
        "decision",
        "infrastructure.cache",
        MemoryType.PROJECT,
    ),
    (
        "Feature flags for risky changes",
        "Use local feature flags for destructive migration paths.",
        "decision",
        "release.feature-flags",
        MemoryType.PROJECT,
    ),
]


def seed(database: Database) -> None:
    service = MemoryService(database, database.settings)
    with database.session() as session:
        if not session.scalar(select(RepositoryRow).where(RepositoryRow.stable_key == "memoryos")):
            session.add(
                RepositoryRow(
                    stable_key="memoryos",
                    name="memoryos",
                    path=str(Path.cwd().resolve()),
                    remote_url="https://example.invalid/local/memoryos.git",
                    default_branch="main",
                )
            )
        existing = int(session.scalar(select(func.count()).select_from(MemoryRow)) or 0)
    if existing:
        return
    for title, content, category, key, memory_type in ACTIVE_FIXTURES:
        service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key="memoryos",
                memory_type=memory_type,
                category=category,
                key=key,
                title=title,
                content=content,
                confidence=0.92,
                importance=0.82 if category in {"decision", "constraint", "failure"} else 0.62,
                created_by=CreatedBy.MANUAL,
                activate_immediately=True,
                source=SourceCreate(
                    source_type=SourceType.GIT_COMMIT,
                    source_ref=f"fixture:{key}",
                    excerpt=content,
                    metadata={"commit": "b2c3d4e", "branch": "main"},
                ),
            ),
            actor="fixture",
        )
    for title, content, category, key, memory_type in CANDIDATE_FIXTURES:
        service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key="memoryos",
                memory_type=memory_type,
                category=category,
                key=key,
                title=title,
                content=content,
                confidence=0.81,
                importance=0.74,
                created_by=CreatedBy.AGENT,
                source=SourceCreate(
                    source_type=SourceType.AGENT,
                    source_ref=f"fixture:candidate:{key}",
                    excerpt=content,
                    metadata={"agent": "codex", "branch": "main"},
                ),
            ),
            actor="fixture",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = settings_for(args.data_dir)
    database = Database(settings)
    database.initialize()
    try:
        seed(database)
        print(service_status(database))
    finally:
        database.close()


def service_status(database: Database) -> str:
    with database.session() as session:
        count = int(session.scalar(select(func.count()).select_from(MemoryRow)) or 0)
    return f"Seeded MemoryOS fixture: {count} memories"


if __name__ == "__main__":
    main()
