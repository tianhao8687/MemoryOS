from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from memoryos.db.models import EmbeddingRow, RetrievalRunRow
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ConflictStrategy,
    ContextRequest,
    CreatedBy,
    ScopeType,
    SearchRequest,
)
from memoryos.engine import MemoryService
from memoryos.errors import ProviderError
from memoryos.providers.base import ProviderMetadata
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2 import RetrievalPipeline
from memoryos.retrieval_v2.diversity import mmr_select
from memoryos.retrieval_v2.fusion import reciprocal_rank_fusion


class FailingEmbeddingProvider:
    name = "failing"
    model = "fixture"

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata("failing", self.model, False, 1000, ("embedding",))

    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise ProviderError("fixture embedding failure")

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)


@pytest.mark.v2
def test_mmr_retains_relevance_while_avoiding_duplicate_context() -> None:
    candidates = [
        {"memory": {"id": "a", "title": "alpha beta", "content": ""}, "fused_score": 1.0},
        {"memory": {"id": "b", "title": "alpha beta", "content": ""}, "fused_score": 0.99},
        {"memory": {"id": "c", "title": "gamma delta", "content": ""}, "fused_score": 0.9},
    ]

    selected = mmr_select(candidates, limit=2, lambda_relevance=0.5)

    assert [item["memory"]["id"] for item in selected] == ["a", "c"]


@pytest.mark.v2
def test_a23_retrieval_trace_and_context_manifest_are_persisted(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    service.propose(
        make_memory(
            title="Current backend decision",
            content="The backend framework uses FastAPI.",
            key="architecture.backend",
        ),
        actor="test",
    )
    service.propose(
        make_memory(
            title="No Redis constraint",
            content="Do not add Redis to this repository.",
            category="constraint",
            key="architecture.cache.constraint",
            source_ref="manual:constraint",
        ),
        actor="test",
    )
    result = service.context(
        ContextRequest(
            task="What is the current architecture constraint for FastAPI and Redis?",
            repository="repo-a",
        )
    )
    assert result["retrieval_mode"].startswith("rrf-")
    assert result["retrieval_run_id"]
    assert result["query_plan"]["intent"] in {"current_decision", "constraint_lookup"}
    assert result["manifest"]
    trace = result["manifest"][0]["retrieval_trace"]
    assert set(trace) == {
        "fts_rank",
        "vector_rank",
        "graph_rank",
        "temporal_rank",
        "fused_score",
        "scope_match",
        "freshness",
        "evidence_count",
        "reranker_score",
        "final_reason",
    }
    with database.session() as session:
        run = session.get(RetrievalRunRow, result["retrieval_run_id"])
        assert run is not None
        assert run.context_manifest
        assert run.selected_memory_ids


@pytest.mark.v2
def test_a24_rrf_and_provider_failure_fall_back_without_database_pollution(
    database: Database, service: MemoryService, make_memory: Any
) -> None:
    memory = service.propose(make_memory(), actor="test")
    scores, traces = reciprocal_rank_fusion({"fts": [memory["id"]], "vector": [], "graph": []})
    assert scores[memory["id"]] > 0
    assert traces[memory["id"]]["fts"] == 1
    pipeline = RetrievalPipeline(database, RetrievalEngine(database, FailingEmbeddingProvider()))
    result = pipeline.search(SearchRequest(query="FastAPI", limit=5))
    assert result["total"] == 1
    assert "fallback" in result["mode"]
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(EmbeddingRow)) == 0


@pytest.mark.v2
def test_a25_context_never_silently_selects_one_side_of_contested_truth(
    service: MemoryService, make_memory: Any
) -> None:
    current = service.propose(
        make_memory(
            title="Use FastAPI",
            content="The backend framework uses FastAPI.",
            key="decision.framework.a",
        ),
        actor="test",
    )
    candidate = service.propose(
        make_memory(
            title="Use Django",
            content="The backend framework uses Django.",
            key="decision.framework.b",
            created_by=CreatedBy.AGENT,
            activate_immediately=False,
            source_ref="agent:contested",
        ),
        actor="test",
    )
    service.confirm(
        candidate["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        rationale="Unresolved migration decision",
        actor="test",
    )
    result = service.context(
        ContextRequest(task="current backend framework FastAPI Django", repository="repo-a")
    )
    included = {item["memory_id"] for item in result["manifest"] if item["included"]}
    assert {current["id"], candidate["id"]}.issubset(included)
    assert result["truth_state"] == "contested"
    assert result["text"].count("CONTESTED:") >= 2


@pytest.mark.v2
def test_narrowing_branch_scope_never_leaks_sibling_branch_memory(
    service: MemoryService, make_memory: Any
) -> None:
    repository = service.propose(
        make_memory(
            title="Repository cache constraint",
            content="Do not add Redis to this repository.",
            category="constraint",
            key="cache.repository",
        ),
        actor="test",
    )
    main = service.propose(
        make_memory(
            title="Main branch cache constraint",
            content="Do not add Redis on the main branch.",
            scope_type=ScopeType.BRANCH,
            scope_key="repo-a:main",
            category="constraint",
            key="cache.main",
        ),
        actor="test",
    )
    sibling = service.propose(
        make_memory(
            title="Experimental branch Redis state",
            content="Use Redis only on the experimental branch.",
            scope_type=ScopeType.BRANCH,
            scope_key="repo-a:experimental",
            key="cache.experimental",
        ),
        actor="test",
    )

    result = service.context(
        ContextRequest(task="current Redis cache constraint", repository="repo-a", branch="main")
    )
    visible = {item["memory_id"] for item in result["manifest"]}
    included = {item["memory_id"] for item in result["manifest"] if item["included"]}

    assert {repository["id"], main["id"]}.issubset(included)
    assert sibling["id"] not in visible
