from __future__ import annotations

from typing import Any

from sqlalchemy import select

from memoryos.db.models import MemorySourceRow, SourceRow
from memoryos.domain.schemas import ContextRequest, SearchRequest
from memoryos.retrieval.search import RetrievalEngine

SECTION_ORDER = [
    "CURRENT DECISIONS",
    "ACTIVE CONSTRAINTS",
    "KNOWN FAILURES / DO NOT REPEAT",
    "RELEVANT PREFERENCES",
    "CURRENT BRANCH / TASK STATE",
    "HISTORICAL / SUPERSEDED",
]


def _section(memory: dict[str, Any]) -> str:
    if memory["status"] != "active":
        return "HISTORICAL / SUPERSEDED"
    category = str(memory["category"]).lower()
    if category == "decision":
        return "CURRENT DECISIONS"
    if category == "constraint":
        return "ACTIVE CONSTRAINTS"
    if category == "failure":
        return "KNOWN FAILURES / DO NOT REPEAT"
    if category == "preference" or memory["memory_type"] == "preference":
        return "RELEVANT PREFERENCES"
    return "CURRENT BRANCH / TASK STATE"


class ContextBuilder:
    def __init__(self, retrieval: RetrievalEngine) -> None:
        self.retrieval = retrieval

    def build(self, request: ContextRequest) -> dict[str, Any]:
        allowed: set[tuple[str, str | None]] = {("user", None), ("repository", request.repository)}
        if request.workspace:
            allowed.add(("workspace", request.workspace))
        if request.branch:
            allowed.add(("branch", f"{request.repository}:{request.branch}"))
            allowed.add(("branch", request.branch))
        if request.task_scope:
            allowed.add(("task", request.task_scope))

        result = self.retrieval.search(
            SearchRequest(
                query=request.task,
                include_history=request.include_historical,
                limit=150,
            ),
            allowed_scopes=allowed,
        )
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_ORDER}
        seen: set[tuple[str, str]] = set()
        budget_used = len("Project Memory Context\n")
        memory_ids = [item["memory"]["id"] for item in result["items"]]
        provenance: dict[str, str] = {}
        if memory_ids:
            with self.retrieval.database.session() as session:
                source_rows = session.execute(
                    select(MemorySourceRow.memory_id, SourceRow.source_ref)
                    .join(SourceRow, SourceRow.id == MemorySourceRow.source_id)
                    .where(MemorySourceRow.memory_id.in_(memory_ids))
                    .order_by(SourceRow.captured_at.desc())
                )
                for memory_id, source_ref in source_rows:
                    provenance.setdefault(memory_id, source_ref)
        for item in result["items"]:
            memory = item["memory"]
            dedupe_key = (str(memory.get("key") or memory.get("subject") or ""), memory["content"])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            source_ref = provenance.get(memory["id"], "unknown")
            entry = {
                **memory,
                "score": item["score"],
                "provenance_ref": source_ref,
            }
            line = (
                f"- [{memory['id']}] {memory['title']}: {memory['content']} "
                f"(source: {source_ref})\n"
            )
            if budget_used + len(line) > request.budget:
                continue
            section = _section(memory)
            sections[section].append(entry)
            budget_used += len(line)

        formatted = ["Project Memory Context"]
        for name in SECTION_ORDER:
            if name == "HISTORICAL / SUPERSEDED" and not request.include_historical:
                continue
            formatted.append(f"\n{name}")
            items = sections[name]
            if not items:
                formatted.append("- None relevant")
            else:
                for item in items:
                    formatted.append(
                        f"- [{item['id']}] {item['title']}: {item['content']} "
                        f"(source: {item['provenance_ref']})"
                    )
        return {
            "task": request.task,
            "repository": request.repository,
            "branch": request.branch,
            "budget": request.budget,
            "characters_used": min(budget_used, request.budget),
            "retrieval_mode": result["mode"],
            "sections": {key: value for key, value in sections.items() if value},
            "text": "\n".join(formatted),
        }
