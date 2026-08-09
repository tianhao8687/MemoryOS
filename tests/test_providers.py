from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from memoryos.db.models import EmbeddingRow, MemoryRow
from memoryos.db.session import Database
from memoryos.domain.schemas import SearchRequest
from memoryos.engine import MemoryService
from memoryos.errors import ProviderError
from memoryos.providers.heuristic import HeuristicExtractor
from memoryos.providers.openai_compatible import OpenAICompatibleExtractor


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": "not-json"}}]}


class DeterministicEmbedding:
    name = "test"
    model = "two-dimensional"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "FastAPI" in text else [0.0, 1.0] for text in texts]


class FailingEmbedding:
    name = "test"
    model = "unavailable"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError("embedding provider unavailable")


def test_heuristic_extractor_works_offline_and_only_returns_candidates() -> None:
    candidates = HeuristicExtractor().extract(
        "We decided to use FastAPI. Do not introduce Redis in V1. The previous session fix failed."
    )
    assert len(candidates) == 3
    assert {candidate.category for candidate in candidates} == {"decision", "constraint", "failure"}


def test_invalid_provider_json_does_not_pollute_database(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: FakeResponse())
    extractor = OpenAICompatibleExtractor(base_url="http://provider.invalid", model="test")
    with pytest.raises(ProviderError):
        extractor.extract("Decide to use FastAPI")
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(MemoryRow)) == 0


def test_provider_timeout_does_not_pollute_database(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    def time_out(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ReadTimeout("provider timed out")

    monkeypatch.setattr("httpx.post", time_out)
    extractor = OpenAICompatibleExtractor(base_url="http://provider.invalid", model="test")
    with pytest.raises(ProviderError):
        extractor.extract("Decide to use FastAPI")
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(MemoryRow)) == 0


def test_active_memory_is_indexed_and_hybrid_search_is_available(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    service.retrieval.embedding_provider = DeterministicEmbedding()
    service.propose(make_memory(), actor="test")
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(EmbeddingRow)) == 1
    result = service.search(SearchRequest(query="FastAPI"))
    assert result["mode"] == "hybrid"
    assert result["total"] == 1


def test_embedding_failure_falls_back_to_fts5(service: MemoryService, make_memory: Any) -> None:
    service.propose(make_memory(), actor="test")
    service.propose(
        make_memory(
            title="Use SQLite",
            content="Use SQLite for local persistence.",
            key="architecture.database",
        ),
        actor="test",
    )
    service.retrieval.embedding_provider = FailingEmbedding()
    result = service.search(SearchRequest(query="FastAPI"))
    assert result["mode"] == "fts5-fallback"
    assert result["total"] == 1
    assert result["items"][0]["memory"]["title"] == "Use FastAPI"
