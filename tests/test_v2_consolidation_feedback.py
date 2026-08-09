from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memoryos.domain.schemas import (
    ConflictStrategy,
    ConsolidateRequest,
    ContextRequest,
    CreatedBy,
    FeedbackCreate,
    FeedbackValue,
    MemoryCreate,
    MemoryType,
    ScopeType,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService


def _episode(*, technology: str, captured_at: datetime, source_ref: str) -> MemoryCreate:
    content = f"We prefer {technology} for package management."
    return MemoryCreate(
        scope_type=ScopeType.REPOSITORY,
        scope_key="repo-consolidation",
        memory_type=MemoryType.EPISODIC,
        category="preference",
        key=f"episode.package-manager.{source_ref}",
        title=f"Package manager episode: {technology}",
        content=content,
        created_by=CreatedBy.MANUAL,
        activate_immediately=True,
        source=SourceCreate(
            source_type=SourceType.CONVERSATION,
            source_ref=source_ref,
            captured_at=captured_at,
            excerpt=content,
        ),
    )


@pytest.mark.v2
def test_a26_consolidation_proposes_candidate_with_complete_lineage(
    service: MemoryService,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    memories = [
        service.propose(
            _episode(
                technology="pnpm",
                captured_at=start + timedelta(days=index * 7),
                source_ref=f"episode:{index}",
            ),
            actor="test",
        )
        for index in range(3)
    ]
    result = service.consolidate(
        ConsolidateRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-consolidation",
        )
    )
    assert result["dry_run"] is True
    assert result["count"] == 1
    proposal = result["proposals"][0]
    assert proposal["status"] == "candidate"
    assert set(proposal["source_memory_ids"]) == {item["id"] for item in memories}
    assert {relation["relation_type"] for relation in proposal["relations"]} == {
        "consolidated_from"
    }
    assert all(service.get(item["id"])["status"] == "active" for item in memories)


@pytest.mark.v2
def test_a27_recent_counterevidence_makes_consolidation_contested(
    service: MemoryService,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    for index in range(3):
        service.propose(
            _episode(
                technology="pnpm",
                captured_at=start + timedelta(days=index * 7),
                source_ref=f"support:{index}",
            ),
            actor="test",
        )
    counter_payload = _episode(
        technology="npm",
        captured_at=start + timedelta(days=21),
        source_ref="counter:recent",
    ).model_copy(update={"created_by": CreatedBy.AGENT, "activate_immediately": False})
    counter = service.propose(
        counter_payload,
        actor="test",
    )
    service.confirm(
        counter["id"],
        strategy=ConflictStrategy.KEEP_BOTH,
        rationale="Counterevidence must remain contested",
        actor="test",
    )
    result = service.consolidate(
        ConsolidateRequest(
            scope_type=ScopeType.REPOSITORY,
            scope_key="repo-consolidation",
            dry_run=False,
        )
    )
    assert result["proposals"][0]["status"] == "contested"
    assert result["proposals"][0]["id"]
    assert counter["id"] in {
        item["memory_id"] for item in result["proposals"][0]["counterevidence"]
    }


@pytest.mark.v2
def test_a28_feedback_is_auditable_utility_only_signal(
    service: MemoryService,
) -> None:
    memory = service.propose(
        _episode(
            technology="pnpm",
            captured_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_ref="feedback:episode",
        ),
        actor="test",
    )
    context = service.context(
        ContextRequest(task="package manager pnpm preference", repository="repo-consolidation")
    )
    feedback = service.feedback(
        FeedbackCreate(
            retrieval_run_id=context["retrieval_run_id"],
            memory_id=memory["id"],
            helpful=FeedbackValue.NO,
            actor="agent-test",
            reason="This episode was not useful for the current task.",
        )
    )
    assert feedback["fact_status_changed"] is False
    assert service.get(memory["id"])["status"] == "active"
    audit = service.explain(memory["id"])["audit"]
    assert audit[-1]["action"] == "memory_feedback"
    assert audit[-1]["details"]["fact_status_changed"] is False
