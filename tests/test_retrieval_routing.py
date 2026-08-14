from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from memoryos.db.session import Database
from memoryos.domain.schemas import QueryIntent, ScopeType, SearchRequest
from memoryos.engine import MemoryService
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2 import RetrievalPipeline
from memoryos.retrieval_v2.planner import plan_query
from memoryos.retrieval_v2.routing import (
    APPROVED_RETRIEVAL_RECIPES,
    SAFE_RECIPE_ID,
    RetrievalRoute,
    RetrievalRoutingShadowProfile,
    extract_exact_terms,
    load_routing_shadow_profile,
    recipe_registry_digest,
    select_retrieval_recipe,
)
from memoryos.retrieval_v2.scoring import CALIBRATABLE_FEATURES, ShadowRetrievalProfile


def _git(repository: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run(  # noqa: S603 - executable and fixture arguments are test-controlled
        [executable, *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("query", "intent", "recipe_id", "route"),
    [
        (
            "Where is `compile_context()` implemented?",
            QueryIntent.IMPLEMENTATION_LOCATION,
            "exact-symbol-v1",
            RetrievalRoute.EXACT,
        ),
        (
            "Why does FastAPI depend on Redis?",
            QueryIntent.WHY_DECISION,
            "relational-graph-v1",
            RetrievalRoute.RELATIONAL,
        ),
        (
            "What was the cache decision as of last week?",
            QueryIntent.HISTORICAL_AS_OF,
            "temporal-as-of-v1",
            RetrievalRoute.TEMPORAL,
        ),
        (
            "What is the current architecture decision?",
            QueryIntent.CURRENT_DECISION,
            "semantic-hybrid-v1",
            RetrievalRoute.SEMANTIC,
        ),
        (
            "Find context",
            QueryIntent.BROAD_SEARCH,
            SAFE_RECIPE_ID,
            RetrievalRoute.SAFE_FALLBACK,
        ),
    ],
)
def test_router_selects_only_approved_recipes(
    query: str,
    intent: QueryIntent,
    recipe_id: str,
    route: RetrievalRoute,
) -> None:
    decision = select_retrieval_recipe(query, intent=intent)

    assert decision.recommended_recipe_id == recipe_id
    assert decision.route is route
    assert decision.recommended_recipe_sha256 == APPROVED_RETRIEVAL_RECIPES[recipe_id].digest()


def test_complex_queries_use_bounded_broad_recipe() -> None:
    decision = select_retrieval_recipe(
        "Where is the cache implemented? Why was it selected?",
        intent=QueryIntent.IMPLEMENTATION_LOCATION,
    )

    assert decision.route is RetrievalRoute.COMPLEX
    assert decision.recommended_recipe_id == "complex-hybrid-v1"


def test_router_exposes_structured_features_and_conservative_fallback() -> None:
    assert extract_exact_terms("Open `src/context.py` and call compile_context().") == (
        "compile_context",
        "src/context.py",
    )
    decision = select_retrieval_recipe(
        "Find useful context",
        intent=QueryIntent.BROAD_SEARCH,
        entities=("fastapi",),
    )

    assert decision.fallback_used is True
    assert decision.decision_basis == "safe_fallback"
    assert decision.reason_codes == ("unclassified_safe_fallback",)
    assert decision.features.intent_reason_code == "caller_supplied_intent"
    assert decision.features.entity_count == 1


def test_unclassified_multi_clause_query_does_not_expand_into_complex_execution() -> None:
    decision = select_retrieval_recipe(
        "Find useful context? Also help me?",
        intent=QueryIntent.BROAD_SEARCH,
    )

    assert decision.recommended_recipe_id == SAFE_RECIPE_ID
    assert decision.decision_basis == "safe_fallback"


def test_routing_shadow_is_bound_to_exact_approved_registry() -> None:
    profile = RetrievalRoutingShadowProfile()

    assert profile.production_eligible is False
    assert profile.production_behavior_changed is False
    assert profile.recipe_registry_sha256 == recipe_registry_digest()
    with pytest.raises(ValidationError, match="registry digest"):
        RetrievalRoutingShadowProfile.model_validate(
            {
                **profile.model_dump(mode="json"),
                "recipe_registry_sha256": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="exactly the approved recipe"):
        RetrievalRoutingShadowProfile.model_validate(
            {
                **profile.model_dump(mode="json"),
                "allowed_recipe_ids": [SAFE_RECIPE_ID],
            }
        )


def test_routing_shadow_profile_round_trips_through_strict_loader(tmp_path: Path) -> None:
    profile = RetrievalRoutingShadowProfile()
    profile_path = tmp_path / "routing-shadow.json"
    profile_path.write_text(
        json.dumps(profile.model_dump(mode="json")),
        encoding="utf-8",
    )

    loaded = load_routing_shadow_profile(profile_path)

    assert loaded == profile
    assert loaded.digest() == profile.digest()


def test_planner_records_advisory_route_without_activating_it() -> None:
    plan = plan_query("Where is `compile_context()` implemented?", repository="repo-a")
    payload = plan.model_dump()

    assert payload["routing"]["recommended_recipe_id"] == "exact-symbol-v1"
    assert payload["routing"]["route"] == "exact"
    assert payload["intent_reason_code"] == "implementation_keyword"
    assert "confidence" not in payload


def test_production_keeps_frozen_safe_recipe(
    service: MemoryService,
    make_memory: Any,
) -> None:
    service.propose(make_memory(title="Compile context", content="compile_context()"), actor="test")

    result = service.retrieval_v2.search(
        SearchRequest(
            query="Where is `compile_context()` implemented?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=5,
        )
    )

    routing = result["query_plan"]["routing"]
    assert routing["recommended_recipe_id"] == "exact-symbol-v1"
    assert routing["execution_mode"] == "frozen_production_baseline"
    assert routing["executed_recipe_id"] == SAFE_RECIPE_ID
    assert routing["requested_channels"] == ["fts", "vector", "graph", "temporal"]
    assert routing["executed_channels"] == ["fts"]
    assert {item["channel"]: item["status"] for item in routing["channel_execution"]} == {
        "fts": "executed",
        "vector": "unavailable",
        "source_anchor": "not_requested",
        "graph": "not_applicable",
        "temporal": "not_applicable",
    }
    assert result["pipeline_mode"].startswith("rrf-")
    assert result["routing_profile_sha256"] is None


def test_explicit_shadow_executes_exact_recipe_without_mmr(
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    service.propose(make_memory(title="Compile context", content="compile_context()"), actor="test")
    profile = RetrievalRoutingShadowProfile()
    shadow = RetrievalPipeline(
        database,
        RetrievalEngine(database),
        routing_profile=profile,
    )

    result = shadow.search(
        SearchRequest(
            query="Where is `compile_context()` implemented?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=5,
        )
    )

    routing = result["query_plan"]["routing"]
    assert routing["execution_mode"] == "candidate_shadow"
    assert routing["executed_recipe_id"] == "exact-symbol-v1"
    assert routing["active_channels"] == ["fts", "vector", "source_anchor"]
    assert routing["reranker_policy"] == "disabled"
    assert routing["reranker_mode"] == "disabled"
    assert routing["diversity_policy"] == "disabled"
    assert routing["score_contract"] == "normalized_weighted_rrf_v1"
    assert routing["fusion_weights"] == {
        "fts": 1.0,
        "vector": 1.0,
        "source_anchor": 1.0,
    }
    assert routing["requested_channels"] == ["fts", "vector", "source_anchor"]
    assert routing["executed_channels"] == ["fts", "source_anchor"]
    assert routing["degraded_channels"] == ["vector"]
    assert result["pipeline_mode"].startswith("routing-shadow-exact-")
    assert result["routing_profile_sha256"] == profile.digest()
    assert all("mmr_score" not in item for item in result["items"])


def test_exact_recipe_uses_structured_source_anchor_channel(
    tmp_path: Path,
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    repository = tmp_path / "source-anchor-repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "routing@example.invalid")
    _git(repository, "config", "user.name", "Routing Fixture")
    source = repository / "context.py"
    source.write_text("def compile_context():\n    return 'context'\n", encoding="utf-8")
    _git(repository, "add", "context.py")
    _git(repository, "commit", "-m", "add context symbol")
    memory = service.propose(
        make_memory(
            title="Context builder location",
            content="The context builder lives in the implementation module.",
        ),
        actor="test",
    )
    service.create_source_anchor(
        memory_id=memory["id"],
        repository_path=str(repository),
        path="context.py",
        symbol_fqn="compile_context",
    )
    shadow = RetrievalPipeline(
        database,
        RetrievalEngine(database),
        routing_profile=RetrievalRoutingShadowProfile(),
    )

    result = shadow.search(
        SearchRequest(
            query="Where is `compile_context()` implemented?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=5,
        )
    )

    assert result["items"][0]["memory"]["id"] == memory["id"]
    assert result["items"][0]["trace"]["source_anchor_rank"] == 1
    routing = result["query_plan"]["routing"]
    assert "source_anchor" in routing["executed_channels"]
    assert "source_anchor" in routing["contributing_channels"]
    anchor_execution = next(
        item for item in routing["channel_execution"] if item["channel"] == "source_anchor"
    )
    assert anchor_execution["status"] == "executed"
    assert anchor_execution["eligible_candidate_count"] == 1


class _FixtureReranker:
    name = "fixture-cross-encoder"

    def __init__(self, preferred_id: str) -> None:
        self.calls = 0
        self.preferred_id = preferred_id

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
        del query
        self.calls += 1
        return {
            str(item["memory"]["id"]): float(str(item["memory"]["id"]) == self.preferred_id)
            for item in candidates
        }


class _PartialReranker:
    name = "partial-cross-encoder"

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
        del query
        return {str(candidates[0]["memory"]["id"]): 1.0} if candidates else {}


class _OutOfContractReranker:
    name = "out-of-contract-cross-encoder"

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
        del query
        return {str(item["memory"]["id"]): 1.01 for item in candidates}


def test_routed_cross_encoder_is_an_authoritative_bounded_stage(
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    first = service.propose(
        make_memory(title="Architecture decision one", content="current architecture decision"),
        actor="test",
    )
    second = service.propose(
        make_memory(
            title="Architecture decision two",
            content="current architecture decision alternative",
            key="architecture.second",
        ),
        actor="test",
    )
    reranker = _FixtureReranker(second["id"])
    shadow = RetrievalPipeline(
        database,
        RetrievalEngine(database),
        reranker,
        routing_profile=RetrievalRoutingShadowProfile(),
    )

    result = shadow.search(
        SearchRequest(
            query="What is the current architecture decision?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=2,
        )
    )

    assert reranker.calls == 1
    assert result["query_plan"]["routing"]["executed_recipe_id"] == "semantic-hybrid-v1"
    assert result["reranker"] == reranker.name
    assert [item["memory"]["id"] for item in result["items"]] == [second["id"], first["id"]]


def test_routed_reranker_fails_back_when_the_bounded_window_is_incomplete(
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    service.propose(
        make_memory(title="Architecture decision one", content="current architecture decision"),
        actor="test",
    )
    service.propose(
        make_memory(
            title="Architecture decision two",
            content="current architecture decision alternative",
            key="architecture.second",
        ),
        actor="test",
    )
    shadow = RetrievalPipeline(
        database,
        RetrievalEngine(database),
        _PartialReranker(),
        routing_profile=RetrievalRoutingShadowProfile(),
    )

    result = shadow.search(
        SearchRequest(
            query="What is the current architecture decision?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=2,
        )
    )

    assert result["reranker"] == "provider-fallback"
    assert all(item["trace"]["reranker_score"] is None for item in result["items"])


def test_routed_reranker_fails_back_on_out_of_contract_scores(
    database: Database,
    service: MemoryService,
    make_memory: Any,
) -> None:
    service.propose(
        make_memory(title="Architecture decision", content="current architecture decision"),
        actor="test",
    )
    shadow = RetrievalPipeline(
        database,
        RetrievalEngine(database),
        _OutOfContractReranker(),
        routing_profile=RetrievalRoutingShadowProfile(),
    )

    result = shadow.search(
        SearchRequest(
            query="What is the current architecture decision?",
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-a",
            limit=1,
        )
    )

    assert result["reranker"] == "provider-fallback"
    assert result["items"][0]["trace"]["reranker_score"] is None


def test_shadow_profiles_cannot_be_composed(database: Database) -> None:
    routing = RetrievalRoutingShadowProfile()

    with pytest.raises(ValueError, match="only one shadow profile"):
        RetrievalPipeline(
            database,
            RetrievalEngine(database),
            routing_profile=routing,
            scoring_profile=ShadowRetrievalProfile(
                source_profile_sha256="a" * 64,
                training_protocol_sha256="b" * 64,
                weights={name: 0.0 for name in CALIBRATABLE_FEATURES},
                mmr_lambda=1.0,
            ),
        )
