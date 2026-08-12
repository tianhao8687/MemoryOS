from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from memoryos.domain.schemas import QueryIntent
from memoryos.entities.aliases import KNOWN_ALIASES, normalize_entity_name
from memoryos.retrieval_v2.routing import (
    RoutingDecision,
    extract_exact_terms,
    select_retrieval_recipe,
)

TECHNOLOGIES = {
    "postgres",
    "postgresql",
    "sqlite",
    "mysql",
    "redis",
    "fastapi",
    "django",
    "react",
    "pnpm",
    "npm",
    "yarn",
    "python",
    "typescript",
    "javascript",
    "rust",
}
IntentReasonCode = Literal[
    "temporal_bounds",
    "temporal_keyword",
    "why_keyword",
    "constraint_keyword",
    "failure_keyword",
    "implementation_keyword",
    "preference_keyword",
    "current_decision_keyword",
    "task_state_keyword",
    "unclassified",
]


@dataclass(frozen=True)
class QueryPlan:
    intent: QueryIntent
    intent_reason_code: IntentReasonCode
    entities: list[str]
    scope_chain: list[str]
    as_of_valid_time: datetime | None
    as_known_at: datetime | None
    requested_evidence_type: str
    exact_terms: list[str]
    routing: RoutingDecision

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intent"] = self.intent.value
        payload["as_of_valid_time"] = (
            self.as_of_valid_time.isoformat() if self.as_of_valid_time else None
        )
        payload["as_known_at"] = self.as_known_at.isoformat() if self.as_known_at else None
        payload["routing"] = self.routing.model_dump(mode="json")
        return payload


def _intent(query: str, has_temporal: bool) -> tuple[QueryIntent, IntentReasonCode]:
    lower = query.lower()
    if has_temporal:
        return QueryIntent.HISTORICAL_AS_OF, "temporal_bounds"
    if any(token in lower for token in ("as of", "historical", "当时", "历史")):
        return QueryIntent.HISTORICAL_AS_OF, "temporal_keyword"
    if any(token in lower for token in ("why", "reason", "because", "为什么", "原因")):
        return QueryIntent.WHY_DECISION, "why_keyword"
    if any(
        token in lower for token in ("do not", "must not", "constraint", "禁止", "不得", "约束")
    ):
        return QueryIntent.CONSTRAINT_LOOKUP, "constraint_keyword"
    if any(token in lower for token in ("failed", "failure", "mistake", "失败", "踩坑")):
        return QueryIntent.FAILURE_HISTORY, "failure_keyword"
    if any(token in lower for token in ("implemented", "where", "located", "symbol", "实现在哪")):
        return QueryIntent.IMPLEMENTATION_LOCATION, "implementation_keyword"
    if any(token in lower for token in ("prefer", "preference", "偏好")):
        return QueryIntent.PREFERENCE, "preference_keyword"
    if any(
        token in lower
        for token in ("current", "decision", "choose", "architecture", "当前", "决定", "架构")
    ):
        return QueryIntent.CURRENT_DECISION, "current_decision_keyword"
    if any(token in lower for token in ("task", "working", "branch", "任务", "分支")):
        return QueryIntent.TASK_STATE, "task_state_keyword"
    return QueryIntent.BROAD_SEARCH, "unclassified"


def _entities(query: str) -> list[str]:
    tokens = re.findall(r"[\w.+#/-]+", query.lower(), re.UNICODE)
    resolved = set()
    for token in tokens:
        normalized = normalize_entity_name(token)
        if normalized in TECHNOLOGIES or token in KNOWN_ALIASES:
            resolved.add(normalized)
        if "/" in token or token.endswith((".py", ".ts", ".tsx", ".js", ".rs")):
            resolved.add(normalized)
    return sorted(resolved)


def plan_query(
    query: str,
    *,
    repository: str | None = None,
    branch: str | None = None,
    workspace: str | None = None,
    task_scope: str | None = None,
    as_of_valid_time: datetime | None = None,
    as_known_at: datetime | None = None,
) -> QueryPlan:
    intent, intent_reason_code = _intent(query, bool(as_of_valid_time or as_known_at))
    entities = _entities(query)
    exact_terms = list(extract_exact_terms(query))
    scope_chain = ["user"]
    if workspace:
        scope_chain.append(f"workspace:{workspace}")
    if repository:
        scope_chain.append(f"repository:{repository}")
    if branch:
        scope_chain.append(f"branch:{repository}:{branch}" if repository else f"branch:{branch}")
    if task_scope:
        scope_chain.append(f"task:{task_scope}")
    evidence = {
        QueryIntent.IMPLEMENTATION_LOCATION: "source_anchor",
        QueryIntent.WHY_DECISION: "provenance",
        QueryIntent.HISTORICAL_AS_OF: "temporal",
    }.get(intent, "claim")
    routing = select_retrieval_recipe(
        query,
        intent=intent,
        entities=entities,
        intent_reason_code=intent_reason_code,
        exact_terms=exact_terms,
    )
    return QueryPlan(
        intent=intent,
        intent_reason_code=intent_reason_code,
        entities=entities,
        scope_chain=scope_chain,
        as_of_valid_time=as_of_valid_time,
        as_known_at=as_known_at,
        requested_evidence_type=evidence,
        exact_terms=exact_terms,
        routing=routing,
    )
