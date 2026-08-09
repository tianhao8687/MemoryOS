from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from memoryos.config import MemoryOSSettings, settings_for
from memoryos.db import Database
from memoryos.domain.schemas import (
    ConflictStrategy,
    ConsolidateRequest,
    ContextRequest,
    CreatedBy,
    CurrentTruthRequest,
    FeedbackCreate,
    FeedbackValue,
    MemoryCreate,
    MemoryStatus,
    MemoryType,
    RefreshRequest,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.errors import MemoryOSError
from memoryos.security.logging import configure_logging


def create_mcp_server(settings: MemoryOSSettings) -> FastMCP:
    configure_logging(settings)
    database = Database(settings)
    database.initialize()
    service = MemoryService(database, settings)
    mcp = FastMCP("MemoryOS", instructions="Local-first, source-backed coding-agent memory")

    def result(call: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "result": call()}
        except MemoryOSError as exc:
            return {"ok": False, "error": exc.as_dict()}
        except ValueError as exc:
            return {
                "ok": False,
                "error": {"code": "VALIDATION_ERROR", "message": str(exc), "details": {}},
            }

    @mcp.tool(name="memory_context")
    def memory_context(
        task: str,
        repo: str,
        branch: str | None = None,
        workspace: str | None = None,
        task_scope: str | None = None,
        budget: int = 6000,
    ) -> dict[str, Any]:
        """Build the most relevant project-memory context for a coding task."""
        return result(
            lambda: service.context(
                ContextRequest(
                    task=task,
                    repository=repo,
                    branch=branch,
                    workspace=workspace,
                    task_scope=task_scope,
                    budget=budget,
                )
            )
        )

    @mcp.tool(name="memory_search")
    def memory_search(
        query: str,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        memory_type: MemoryType | None = None,
        status: MemoryStatus | None = None,
        include_history: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search memory with FTS5 and optional scope, type, and status filters."""
        return result(
            lambda: service.search(
                SearchRequest(
                    query=query,
                    scope_type=scope_type,
                    scope_key=scope_key,
                    memory_type=memory_type,
                    status=status,
                    include_history=include_history,
                    limit=limit,
                )
            )
        )

    @mcp.tool(name="memory_propose")
    def memory_propose(
        title: str,
        content: str,
        scope_type: ScopeType,
        scope_key: str,
        memory_type: MemoryType,
        category: str,
        source_type: SourceType,
        source_ref: str,
        source_excerpt: str,
        key: str | None = None,
        subject: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Submit a source-backed candidate memory. Agent writes never become active directly."""
        return result(
            lambda: service.propose(
                MemoryCreate(
                    title=title,
                    content=content,
                    scope_type=scope_type,
                    scope_key=scope_key,
                    memory_type=memory_type,
                    category=category,
                    source=SourceCreate(
                        source_type=source_type,
                        source_ref=source_ref,
                        excerpt=source_excerpt,
                    ),
                    key=key,
                    subject=subject,
                    confidence=confidence,
                    importance=importance,
                    ttl_seconds=ttl_seconds,
                    created_by=CreatedBy.AGENT,
                ),
                actor="mcp",
            )
        )

    @mcp.tool(name="memory_confirm")
    def memory_confirm(
        memory_id: str,
        strategy: ConflictStrategy | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Confirm a candidate; conflicts require supersede, keep_both, or reject."""
        return result(
            lambda: service.confirm(memory_id, strategy=strategy, actor="mcp", rationale=rationale)
        )

    @mcp.tool(name="memory_forget")
    def memory_forget(memory_id: str) -> dict[str, Any]:
        """Logically forget a memory while retaining its minimal audit history."""
        return result(lambda: service.forget(memory_id, actor="mcp"))

    @mcp.tool(name="memory_history")
    def memory_history(memory_id: str | None = None, key: str | None = None) -> dict[str, Any]:
        """Return the complete status and supersession timeline for a memory or semantic key."""
        return result(lambda: service.history(memory_id=memory_id, key=key))

    @mcp.tool(name="memory_explain")
    def memory_explain(memory_id: str) -> dict[str, Any]:
        """Explain provenance, content hash, scope, creator, status, and replacement links."""
        return result(lambda: service.explain(memory_id))

    @mcp.tool(name="memory_current_truth")
    def memory_current_truth(
        subject: str | None = None,
        predicate: str | None = None,
        query: str | None = None,
        scope_type: ScopeType | None = None,
        scope_key: str | None = None,
        as_of_valid_time: str | None = None,
        as_known_at: str | None = None,
    ) -> dict[str, Any]:
        """Query resolved, contested, stale, or unknown bitemporal project truth."""
        return result(
            lambda: service.current_truth(
                CurrentTruthRequest.model_validate(
                    {
                        "subject": subject,
                        "predicate": predicate,
                        "query": query,
                        "scope_type": scope_type,
                        "scope_key": scope_key,
                        "as_of_valid_time": as_of_valid_time,
                        "as_known_at": as_known_at,
                    }
                )
            )
        )

    @mcp.tool(name="memory_feedback")
    def memory_feedback(
        retrieval_run_id: str,
        memory_id: str,
        helpful: FeedbackValue,
        actor: str = "agent",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record auditable helpful/unhelpful retrieval feedback without changing truth."""
        return result(
            lambda: service.feedback(
                FeedbackCreate(
                    retrieval_run_id=retrieval_run_id,
                    memory_id=memory_id,
                    helpful=helpful,
                    actor=actor,
                    reason=reason,
                )
            )
        )

    @mcp.tool(name="memory_consolidate")
    def memory_consolidate(
        scope_type: ScopeType,
        scope_key: str,
        dry_run: bool = True,
        minimum_sources: int = 3,
        minimum_span_days: int = 7,
    ) -> dict[str, Any]:
        """Generate traceable consolidation candidates; dry-run is the default."""
        return result(
            lambda: service.consolidate(
                ConsolidateRequest(
                    scope_type=scope_type,
                    scope_key=scope_key,
                    dry_run=dry_run,
                    minimum_sources=minimum_sources,
                    minimum_span_days=minimum_span_days,
                )
            )
        )

    @mcp.tool(name="memory_refresh")
    def memory_refresh(
        memory_id: str,
        repository_path: str,
        create_replacement_candidate: bool = False,
    ) -> dict[str, Any]:
        """Refresh Git anchor freshness and optionally create a replacement candidate."""
        return result(
            lambda: service.refresh_memory(
                RefreshRequest(
                    memory_id=memory_id,
                    repository_path=repository_path,
                    create_replacement_candidate=create_replacement_candidate,
                )
            )
        )

    @mcp.tool(name="memory_debug_context")
    def memory_debug_context(
        task: str,
        repo: str,
        branch: str | None = None,
        workspace: str | None = None,
        task_scope: str | None = None,
        budget: int = 6000,
        include_historical: bool = False,
    ) -> dict[str, Any]:
        """Return query plan, RRF channels, filters, manifest, and final context selection."""
        return result(
            lambda: service.debug_context(
                ContextRequest(
                    task=task,
                    repository=repo,
                    branch=branch,
                    workspace=workspace,
                    task_scope=task_scope,
                    budget=budget,
                    include_historical=include_historical,
                )
            )
        )

    return mcp


def run_mcp(settings: MemoryOSSettings) -> None:
    create_mcp_server(settings).run(transport="stdio")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=None)
    arguments = parser.parse_args()
    run_mcp(settings_for(arguments.data_dir))


if __name__ == "__main__":
    main()
