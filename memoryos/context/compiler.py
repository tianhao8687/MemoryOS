from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from memoryos.db.models import (
    ClaimRow,
    MemorySourceRow,
    RetrievalRunRow,
    SourceRow,
)
from memoryos.domain.schemas import ContextRequest, QueryIntent, SearchRequest
from memoryos.retrieval.context import SECTION_ORDER, _section
from memoryos.retrieval_v2 import RetrievalPipeline

COVERAGE = {
    QueryIntent.CURRENT_DECISION: ["decision", "constraint", "failure"],
    QueryIntent.CONSTRAINT_LOOKUP: ["constraint", "decision"],
    QueryIntent.FAILURE_HISTORY: ["failure", "decision", "constraint"],
    QueryIntent.WHY_DECISION: ["decision", "failure"],
    QueryIntent.IMPLEMENTATION_LOCATION: ["implementation", "decision"],
    QueryIntent.PREFERENCE: ["preference"],
    QueryIntent.TASK_STATE: ["state", "decision", "constraint"],
    QueryIntent.HISTORICAL_AS_OF: ["decision", "state"],
    QueryIntent.BROAD_SEARCH: [],
}


class TaskAwareContextCompiler:
    def __init__(self, retrieval: RetrievalPipeline) -> None:
        self.retrieval = retrieval

    def build(self, request: ContextRequest) -> dict[str, Any]:
        allowed: set[tuple[str, str | None]] = {
            ("user", None),
            ("repository", request.repository),
        }
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
                as_of_valid_time=request.as_of_valid_time,
                as_known_at=request.as_known_at,
                limit=150,
            ),
            allowed_scopes=allowed,
            task=request.task,
            repository=request.repository,
            branch=request.branch,
            workspace=request.workspace,
            task_scope=request.task_scope,
        )
        candidates = list(result["items"])
        intent = QueryIntent(result["query_plan"]["intent"])
        metadata = self._metadata(candidates)
        manifest = []
        prepared = []
        for item in candidates:
            memory = item["memory"]
            identity = str(memory["id"])
            freshness = str(item["trace"]["freshness"])
            confidence = float(memory.get("confidence", 0.5))
            evidence_factor = 1.0 + min(int(item["trace"]["evidence_count"]), 3) * 0.05
            freshness_factor = {"fresh": 1.0, "unknown": 0.82, "suspect": 0.3, "stale": 0.0}[
                freshness
            ]
            utility = (
                max(float(item["score"]), 0.000001)
                * confidence
                * evidence_factor
                * freshness_factor
            )
            prefix = "CONTESTED: " if item["truth_state"] == "contested" else ""
            if freshness == "suspect":
                prefix += "SUSPECT: "
            source_ref = metadata[identity]["source_ref"]
            line = (
                f"- [{identity}] {prefix}{memory['title']}: {memory['content']} "
                f"(source: {source_ref})"
            )
            prepared.append(
                {
                    "item": item,
                    "memory_id": identity,
                    "utility": utility,
                    "cost": len(line) + 1,
                    "line": line,
                    "category": str(memory["category"]).lower(),
                    "claim_groups": metadata[identity]["claim_groups"],
                    "source_ref": source_ref,
                }
            )
        prepared.sort(
            key=lambda value: float(value["utility"]) / max(1, int(value["cost"])),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        used = len("Project Memory Context\n")

        def include(candidate: dict[str, Any], reason: str, *, force: bool = False) -> bool:
            nonlocal used
            identity = str(candidate["memory_id"])
            if identity in selected_ids:
                return True
            cost = int(candidate["cost"])
            if not force and used + cost > request.budget:
                return False
            selected.append({**candidate, "inclusion_reason": reason})
            selected_ids.add(identity)
            used += cost
            return True

        for category in COVERAGE[intent]:
            match = next((item for item in prepared if item["category"] == category), None)
            if match is not None:
                include(match, f"required coverage: {category}")
        contested_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in prepared:
            if candidate["item"]["truth_state"] == "contested":
                for group in candidate["claim_groups"]:
                    contested_groups[group].append(candidate)
        for group, group_candidates in contested_groups.items():
            if any(str(item["memory_id"]) in selected_ids for item in group_candidates):
                for item in group_candidates:
                    include(item, f"contested group: {group}", force=True)
        for candidate in prepared:
            include(candidate, "highest utility per context cost")

        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_ORDER}
        for selected_item in selected:
            item = selected_item["item"]
            memory = item["memory"]
            section = _section(memory)
            sections[section].append(
                {
                    **memory,
                    "score": item["score"],
                    "provenance_ref": selected_item["source_ref"],
                    "truth_state": item["truth_state"],
                    "freshness": item["trace"]["freshness"],
                    "retrieval_trace": item["trace"],
                }
            )
        for candidate in prepared:
            identity = str(candidate["memory_id"])
            included = identity in selected_ids
            manifest.append(
                {
                    "memory_id": identity,
                    "claim_ids": candidate["item"]["claim_ids"],
                    "included": included,
                    "inclusion_reason": next(
                        (
                            item["inclusion_reason"]
                            for item in selected
                            if item["memory_id"] == identity
                        ),
                        None,
                    ),
                    "exclusion_reason": None if included else "budget or lower utility",
                    "utility": round(float(candidate["utility"]), 8),
                    "cost": candidate["cost"],
                    "truth_state": candidate["item"]["truth_state"],
                    "freshness": candidate["item"]["trace"]["freshness"],
                    "retrieval_trace": candidate["item"]["trace"],
                }
            )
        text = self._format(sections, request.include_historical)
        truth_states = {item["truth_state"] for item in manifest if item["included"]}
        aggregate_truth = (
            "contested"
            if "contested" in truth_states
            else "stale"
            if "stale" in truth_states
            else "resolved"
            if "resolved" in truth_states
            else "unknown"
        )
        with self.retrieval.database.session() as session:
            run = session.get(RetrievalRunRow, result["retrieval_run_id"])
            if run is not None:
                run.context_manifest = manifest
                run.selected_memory_ids = [str(item["memory_id"]) for item in selected]
        return {
            "task": request.task,
            "repository": request.repository,
            "branch": request.branch,
            "budget": request.budget,
            "characters_used": min(used, request.budget),
            "retrieval_mode": result["pipeline_mode"],
            "retrieval_run_id": result["retrieval_run_id"],
            "query_plan": result["query_plan"],
            "truth_state": aggregate_truth,
            "sections": {key: value for key, value in sections.items() if value},
            "manifest": manifest,
            "text": text,
            "debug": {
                "config_hash": result["config_hash"],
                "reranker": result["reranker"],
                "candidates": manifest,
            },
        }

    def _metadata(self, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        memory_ids = [str(item["memory"]["id"]) for item in candidates]
        result: dict[str, dict[str, Any]] = {
            memory_id: {"source_ref": "unknown", "claim_groups": []} for memory_id in memory_ids
        }
        if not memory_ids:
            return result
        with self.retrieval.database.session() as session:
            source_rows = session.execute(
                select(MemorySourceRow.memory_id, SourceRow.source_ref)
                .join(SourceRow, SourceRow.id == MemorySourceRow.source_id)
                .where(MemorySourceRow.memory_id.in_(memory_ids))
                .order_by(SourceRow.captured_at.desc())
            )
            for memory_id, source_ref in source_rows:
                if result[memory_id]["source_ref"] == "unknown":
                    result[memory_id]["source_ref"] = source_ref
            claim_rows = session.execute(
                select(ClaimRow.memory_id, ClaimRow.subject_entity_id, ClaimRow.predicate).where(
                    ClaimRow.memory_id.in_(memory_ids)
                )
            )
            for memory_id, subject_entity_id, predicate in claim_rows:
                result[memory_id]["claim_groups"].append(f"{subject_entity_id}:{predicate}")
        return result

    @staticmethod
    def _format(sections: dict[str, list[dict[str, Any]]], include_historical: bool) -> str:
        formatted = ["Project Memory Context"]
        for name in SECTION_ORDER:
            if name == "HISTORICAL / SUPERSEDED" and not include_historical:
                continue
            formatted.append(f"\n{name}")
            items = sections[name]
            if not items:
                formatted.append("- None relevant")
                continue
            for item in items:
                prefix = "CONTESTED: " if item["truth_state"] == "contested" else ""
                if item["freshness"] == "suspect":
                    prefix += "SUSPECT: "
                formatted.append(
                    f"- [{item['id']}] {prefix}{item['title']}: {item['content']} "
                    f"(source: {item['provenance_ref']})"
                )
        return "\n".join(formatted)
