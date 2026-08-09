from __future__ import annotations

from typing import Any

from memoryos.db.models import (
    AuditEventRow,
    MemoryFeedbackRow,
    MemoryRow,
    RetrievalRunRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import FeedbackCreate
from memoryos.errors import NotFoundError


class FeedbackService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def submit(self, payload: FeedbackCreate) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(RetrievalRunRow, payload.retrieval_run_id)
            memory = session.get(MemoryRow, payload.memory_id)
            if run is None:
                raise NotFoundError("retrieval run was not found")
            if memory is None:
                raise NotFoundError("memory was not found")
            candidate_ids = {
                str(item.get("memory_id"))
                for item in run.candidate_features
                if item.get("memory_id")
            }
            if payload.memory_id not in candidate_ids:
                raise ValueError("feedback memory was not part of the retrieval run")
            row = MemoryFeedbackRow(
                retrieval_run_id=payload.retrieval_run_id,
                memory_id=payload.memory_id,
                helpful=payload.helpful,
                actor=payload.actor,
                reason=payload.reason,
            )
            session.add(row)
            session.add(
                AuditEventRow(
                    action="memory_feedback",
                    entity_type="memory",
                    entity_id=payload.memory_id,
                    actor=payload.actor,
                    details={
                        "retrieval_run_id": payload.retrieval_run_id,
                        "helpful": payload.helpful.value,
                        "reason": payload.reason,
                        "fact_status_changed": False,
                    },
                )
            )
            session.flush()
            return {
                "id": row.id,
                "retrieval_run_id": row.retrieval_run_id,
                "memory_id": row.memory_id,
                "helpful": row.helpful.value,
                "actor": row.actor,
                "reason": row.reason,
                "created_at": row.created_at.isoformat(),
                "fact_status_changed": False,
            }
