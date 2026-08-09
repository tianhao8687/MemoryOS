from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memoryos.config import MemoryOSSettings, settings_for
from memoryos.db import Database
from memoryos.domain.schemas import (
    CreatedBy,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService


@pytest.fixture
def settings(tmp_path: Path) -> MemoryOSSettings:
    return settings_for(tmp_path / "memoryos-data")


@pytest.fixture
def database(settings: MemoryOSSettings) -> Iterator[Database]:
    value = Database(settings)
    value.initialize()
    yield value
    value.close()


@pytest.fixture
def service(database: Database, settings: MemoryOSSettings) -> MemoryService:
    return MemoryService(database, settings)


@pytest.fixture
def make_memory() -> Any:
    def factory(
        *,
        title: str = "Use FastAPI",
        content: str = "The backend uses FastAPI for the login API.",
        scope_type: ScopeType = ScopeType.REPOSITORY,
        scope_key: str = "repo-a",
        memory_type: MemoryType = MemoryType.PROJECT,
        category: str = "decision",
        key: str | None = "architecture.backend.framework",
        created_by: CreatedBy = CreatedBy.MANUAL,
        activate_immediately: bool = True,
        ttl_seconds: int | None = None,
        source_ref: str = "manual:test",
    ) -> MemoryCreate:
        return MemoryCreate(
            scope_type=scope_type,
            scope_key=scope_key,
            memory_type=memory_type,
            category=category,
            key=key,
            title=title,
            content=content,
            confidence=0.9,
            importance=0.8,
            ttl_seconds=ttl_seconds,
            created_by=created_by,
            activate_immediately=activate_immediately,
            source=SourceCreate(
                source_type=SourceType.MANUAL
                if created_by is CreatedBy.MANUAL
                else SourceType.AGENT,
                source_ref=source_ref,
                excerpt=content,
            ),
        )

    return factory
