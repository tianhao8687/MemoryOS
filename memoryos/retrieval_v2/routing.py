from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memoryos.domain.schemas import QueryIntent

RetrievalChannel = Literal["fts", "vector", "source_anchor", "graph", "temporal"]
RerankerPolicy = Literal["disabled", "cross_encoder_if_available"]
DiversityPolicy = Literal["disabled", "mmr"]

ROUTER_VERSION: Literal["approved-recipe-router-v2"] = "approved-recipe-router-v2"
SAFE_RECIPE_ID = "safe-hybrid-v1"
RETRIEVAL_CHANNELS: tuple[RetrievalChannel, ...] = (
    "fts",
    "vector",
    "source_anchor",
    "graph",
    "temporal",
)


class RetrievalRoute(StrEnum):
    EXACT = "exact"
    SEMANTIC = "semantic"
    RELATIONAL = "relational"
    TEMPORAL = "temporal"
    COMPLEX = "complex"
    SAFE_FALLBACK = "safe_fallback"


class RetrievalRecipe(BaseModel):
    """An immutable, allowlisted retrieval execution recipe.

    The router selects recipe identifiers; it never emits free-form weights or
    executable policy. Channel weights remain owned by the frozen RRF baseline
    (or by a separately validated shadow profile).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    recipe_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    route: RetrievalRoute
    channels: tuple[RetrievalChannel, ...]
    fusion: Literal["rrf"] = "rrf"
    reranker_policy: RerankerPolicy
    diversity_policy: DiversityPolicy
    candidate_pool_min: Literal[80] = 80
    candidate_pool_max: Literal[1000] = 1000
    rerank_window: Literal[40] = 40

    @model_validator(mode="after")
    def validate_channels(self) -> RetrievalRecipe:
        if not self.channels:
            raise ValueError("retrieval recipe requires at least one channel")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("retrieval recipe channels must be unique")
        canonical = tuple(channel for channel in RETRIEVAL_CHANNELS if channel in self.channels)
        if self.channels != canonical:
            raise ValueError("retrieval recipe channels must use canonical order")
        return self

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recipe(
    recipe_id: str,
    route: RetrievalRoute,
    channels: tuple[RetrievalChannel, ...],
    *,
    rerank: bool,
    diversify: bool,
) -> RetrievalRecipe:
    return RetrievalRecipe(
        recipe_id=recipe_id,
        route=route,
        channels=channels,
        reranker_policy=("cross_encoder_if_available" if rerank else "disabled"),
        diversity_policy=("mmr" if diversify else "disabled"),
    )


_RECIPES = {
    SAFE_RECIPE_ID: _recipe(
        SAFE_RECIPE_ID,
        RetrievalRoute.SAFE_FALLBACK,
        ("fts", "vector", "graph", "temporal"),
        rerank=True,
        diversify=True,
    ),
    "exact-symbol-v1": _recipe(
        "exact-symbol-v1",
        RetrievalRoute.EXACT,
        ("fts", "vector", "source_anchor"),
        rerank=False,
        diversify=False,
    ),
    "semantic-hybrid-v1": _recipe(
        "semantic-hybrid-v1",
        RetrievalRoute.SEMANTIC,
        ("fts", "vector"),
        rerank=True,
        diversify=True,
    ),
    "relational-graph-v1": _recipe(
        "relational-graph-v1",
        RetrievalRoute.RELATIONAL,
        ("fts", "vector", "graph"),
        rerank=True,
        diversify=True,
    ),
    "temporal-as-of-v1": _recipe(
        "temporal-as-of-v1",
        RetrievalRoute.TEMPORAL,
        ("fts", "vector", "temporal"),
        rerank=True,
        diversify=False,
    ),
    "complex-hybrid-v1": _recipe(
        "complex-hybrid-v1",
        RetrievalRoute.COMPLEX,
        RETRIEVAL_CHANNELS,
        rerank=True,
        diversify=True,
    ),
}

APPROVED_RETRIEVAL_RECIPES = MappingProxyType(_RECIPES)


def recipe_registry_digest() -> str:
    payload = {
        recipe_id: recipe.model_dump(mode="json")
        for recipe_id, recipe in sorted(APPROVED_RETRIEVAL_RECIPES.items())
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RetrievalRoutingShadowProfile(BaseModel):
    """Explicit candidate-only switch for the approved recipe router."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["retrieval_routing_candidate_shadow"] = "retrieval_routing_candidate_shadow"
    router_version: Literal["approved-recipe-router-v2"] = ROUTER_VERSION
    recipe_registry_sha256: str = Field(
        default_factory=recipe_registry_digest,
        pattern=r"^[0-9a-f]{64}$",
    )
    allowed_recipe_ids: tuple[str, ...] = Field(
        default_factory=lambda: tuple(sorted(APPROVED_RETRIEVAL_RECIPES))
    )
    candidate_score_contract: Literal["normalized_weighted_rrf_v1"] = "normalized_weighted_rrf_v1"
    source_anchor_weight_policy: Literal["inherit_fts"] = "inherit_fts"
    production_eligible: Literal[False] = False
    production_behavior_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_registry(self) -> RetrievalRoutingShadowProfile:
        if self.recipe_registry_sha256 != recipe_registry_digest():
            raise ValueError("routing shadow recipe registry digest does not match this runtime")
        if self.allowed_recipe_ids != tuple(sorted(APPROVED_RETRIEVAL_RECIPES)):
            raise ValueError("routing shadow must allow exactly the approved recipe registry")
        return self

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RoutingFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    exact_term_count: int = Field(ge=0)
    entity_count: int = Field(ge=0)
    has_exact_signal: bool
    has_relational_signal: bool
    has_temporal_signal: bool
    clause_count: int = Field(ge=1)


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: RetrievalRoute
    recommended_recipe_id: str
    recommended_recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fallback_used: bool
    decision_basis: Literal["explicit_signals", "planner_intent", "safe_fallback"]
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    router_version: Literal["approved-recipe-router-v2"] = ROUTER_VERSION
    features: RoutingFeatures


_EXACT_PATTERN = re.compile(
    r"(?:"
    r"`[^`]+`|"
    r"\b[\w.-]+\.(?i:py|pyi|js|jsx|ts|tsx|rs|go|java|cpp|c|h|toml|yaml|yml|json)\b|"
    r"(?:^|\s)(?:[A-Za-z]:)?[\\/][\w.\\/-]+|"
    r"\b[A-Za-z_][A-Za-z0-9_]*\(\)|"
    r"\b[A-Z][A-Z0-9_]{2,}\b|"
    r"\b(?i:traceback|stack trace|exception|error code)\b"
    r")",
)
_RELATIONAL_PATTERN = re.compile(
    r"(?:depends? on|dependency|relationship|related to|calls?|imports?|impact|affected by|"
    r"依赖|关系|调用|引用|影响|关联)",
    re.IGNORECASE,
)
_TEMPORAL_PATTERN = re.compile(
    r"(?:as of|at the time|before|after|historical|history|timeline|when did|"
    r"当时|截至|之前|之后|历史|时间线|何时)",
    re.IGNORECASE,
)


_BACKTICK_TERM_PATTERN = re.compile(r"`([^`]+)`")
_FILE_TERM_PATTERN = re.compile(
    r"\b(?:[\w.-]+[\\/])*[\w.-]+\."
    r"(?i:py|pyi|js|jsx|ts|tsx|rs|go|java|cpp|c|h|toml|yaml|yml|json)\b"
)
_PATH_TERM_PATTERN = re.compile(r"(?<![\w.-])(?:[A-Za-z]:)?[\\/][\w.\\/-]+")
_CALL_TERM_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\(\)")
_UPPER_TERM_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def extract_exact_terms(query: str) -> tuple[str, ...]:
    """Extract bounded code/path identifiers for the structured anchor channel."""

    raw_terms = [
        *_BACKTICK_TERM_PATTERN.findall(query),
        *_FILE_TERM_PATTERN.findall(query),
        *_PATH_TERM_PATTERN.findall(query),
        *_CALL_TERM_PATTERN.findall(query),
        *_UPPER_TERM_PATTERN.findall(query),
    ]
    normalized: dict[str, str] = {}
    for raw in raw_terms:
        term = raw.strip().strip("'\"").replace("\\", "/")
        if term.endswith("()"):
            term = term[:-2]
        if not term or len(term) > 1000:
            continue
        normalized.setdefault(term.casefold(), term)
    return tuple(normalized[key] for key in sorted(normalized))


def _clause_count(query: str) -> int:
    question_count = query.count("?") + query.count("\uff1f")
    clause_breaks = len(re.findall(r"[;\uff1b\n]", query))
    return max(1, question_count, clause_breaks + 1)


def select_retrieval_recipe(
    query: str,
    *,
    intent: QueryIntent,
    entities: tuple[str, ...] | list[str] = (),
    intent_reason_code: str = "caller_supplied_intent",
    exact_terms: tuple[str, ...] | list[str] | None = None,
) -> RoutingDecision:
    """Select an approved recipe without producing arbitrary executable values."""

    normalized = query.strip()
    bounded_exact_terms = (
        tuple(exact_terms) if exact_terms is not None else extract_exact_terms(query)
    )
    has_exact = bool(bounded_exact_terms) or intent is QueryIntent.IMPLEMENTATION_LOCATION
    has_temporal = bool(
        intent is QueryIntent.HISTORICAL_AS_OF or _TEMPORAL_PATTERN.search(normalized)
    )
    has_relational = bool(
        intent is QueryIntent.WHY_DECISION or _RELATIONAL_PATTERN.search(normalized)
    )
    primary_signal_count = sum((has_exact, has_temporal, has_relational))
    clause_count = _clause_count(normalized)
    reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    recipe_id: str
    fallback = False
    decision_basis: Literal["explicit_signals", "planner_intent", "safe_fallback"]

    if intent is QueryIntent.BROAD_SEARCH and primary_signal_count == 0:
        recipe_id = SAFE_RECIPE_ID
        reason_codes = ("unclassified_safe_fallback",)
        reasons = ("no explicit routing signal was recognized; use the frozen safe fallback",)
        fallback = True
        decision_basis = "safe_fallback"
    elif clause_count > 1 or primary_signal_count > 1:
        recipe_id = "complex-hybrid-v1"
        reason_codes = ("multi_signal_or_clause",)
        reasons = ("multiple query clauses require broad candidate coverage",)
        decision_basis = "explicit_signals"
    elif has_temporal:
        recipe_id = "temporal-as-of-v1"
        reason_codes = ("temporal_signal",)
        reasons = ("temporal intent requires validity-aware retrieval",)
        decision_basis = (
            "explicit_signals" if _TEMPORAL_PATTERN.search(normalized) else "planner_intent"
        )
    elif has_exact or _EXACT_PATTERN.search(normalized):
        recipe_id = "exact-symbol-v1"
        reason_codes = ("exact_identifier_or_location",)
        reasons = ("exact code or identifier signal detected",)
        decision_basis = "explicit_signals" if bounded_exact_terms else "planner_intent"
    elif has_relational:
        recipe_id = "relational-graph-v1"
        reason_codes = ("relationship_or_provenance",)
        reasons = ("relationship or provenance signal detected",)
        decision_basis = (
            "explicit_signals" if _RELATIONAL_PATTERN.search(normalized) else "planner_intent"
        )
    else:
        recipe_id = "semantic-hybrid-v1"
        reason_codes = ("semantic_intent",)
        reasons = ("natural-language intent uses semantic hybrid retrieval",)
        decision_basis = "planner_intent"

    recipe = APPROVED_RETRIEVAL_RECIPES[recipe_id]
    return RoutingDecision(
        route=recipe.route,
        recommended_recipe_id=recipe.recipe_id,
        recommended_recipe_sha256=recipe.digest(),
        fallback_used=fallback,
        decision_basis=decision_basis,
        reason_codes=reason_codes,
        reasons=reasons,
        features=RoutingFeatures(
            intent_reason_code=intent_reason_code,
            exact_term_count=len(bounded_exact_terms),
            entity_count=len(entities),
            has_exact_signal=has_exact,
            has_relational_signal=has_relational,
            has_temporal_signal=has_temporal,
            clause_count=clause_count,
        ),
    )


def load_routing_shadow_profile(path: Path) -> RetrievalRoutingShadowProfile:
    resolved = path.resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid retrieval routing shadow profile: {resolved}") from exc
    return RetrievalRoutingShadowProfile.model_validate(payload)


__all__ = [
    "APPROVED_RETRIEVAL_RECIPES",
    "ROUTER_VERSION",
    "SAFE_RECIPE_ID",
    "RetrievalRecipe",
    "RetrievalRoute",
    "RetrievalRoutingShadowProfile",
    "RoutingDecision",
    "RoutingFeatures",
    "extract_exact_terms",
    "load_routing_shadow_profile",
    "recipe_registry_digest",
    "select_retrieval_recipe",
]
