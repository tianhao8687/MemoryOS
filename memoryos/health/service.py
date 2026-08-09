from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from memoryos.db.models import (
    AuditEventRow,
    ClaimRow,
    ClaimVersionRow,
    MemoryFeedbackRow,
    MemoryHealthRow,
    MemoryRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ClaimStaleState,
    ClaimStatus,
    FeedbackValue,
    MemoryTemperature,
)
from memoryos.errors import InvalidTransitionError, NotFoundError


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryHealthService:
    """Explainable temperature scoring and reversible, truth-safe archiving."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.database.session() as session:
            memories = list(session.scalars(select(MemoryRow)))
            claims = list(session.scalars(select(ClaimRow)))
            claims_by_memory: dict[str, list[ClaimRow]] = defaultdict(list)
            for claim in claims:
                claims_by_memory[claim.memory_id].append(claim)
            feedback_rows = session.execute(
                select(
                    MemoryFeedbackRow.memory_id,
                    MemoryFeedbackRow.helpful,
                    func.count(MemoryFeedbackRow.id),
                ).group_by(MemoryFeedbackRow.memory_id, MemoryFeedbackRow.helpful)
            ).all()
            feedback: dict[str, Counter[str]] = defaultdict(Counter)
            for memory_id, helpful, count in feedback_rows:
                feedback[memory_id][helpful.value] += int(count)
            evaluated = []
            for memory in memories:
                health = session.get(MemoryHealthRow, memory.id)
                if health is None:
                    health = MemoryHealthRow(
                        memory_id=memory.id,
                        temperature=MemoryTemperature.WARM,
                        health_score=0.5,
                        components_json={},
                        explanation="Pending first health evaluation",
                        retrieval_count=0,
                        evaluated_at=now,
                    )
                    session.add(health)
                components = self._components(
                    memory,
                    claims_by_memory.get(memory.id, []),
                    feedback.get(memory.id, Counter()),
                    health,
                    now,
                )
                score = round(
                    components["recency"] * 0.25
                    + components["usage"] * 0.2
                    + components["feedback"] * 0.2
                    + components["freshness"] * 0.15
                    + components["importance"] * 0.1
                    + components["confidence"] * 0.1,
                    6,
                )
                if health.temperature is not MemoryTemperature.ARCHIVED:
                    if score >= 0.75 or (
                        health.last_retrieved_at is not None
                        and (now - _utc(health.last_retrieved_at)).days <= 7
                    ):
                        health.temperature = MemoryTemperature.HOT
                    elif score >= 0.45:
                        health.temperature = MemoryTemperature.WARM
                    else:
                        health.temperature = MemoryTemperature.COLD
                health.health_score = score
                health.components_json = components
                health.explanation = self._explanation(health.temperature, score, components)
                health.evaluated_at = now
                evaluated.append(self.serialize(health, memory))
            counts = Counter(item["temperature"] for item in evaluated)
            return {"evaluated": len(evaluated), "counts": dict(counts), "items": evaluated}

    def items(self, *, temperature: MemoryTemperature | None = None) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(MemoryHealthRow, MemoryRow).join(
                MemoryRow, MemoryRow.id == MemoryHealthRow.memory_id
            )
            if temperature is not None:
                statement = statement.where(MemoryHealthRow.temperature == temperature)
            rows = session.execute(
                statement.order_by(
                    MemoryHealthRow.temperature,
                    MemoryHealthRow.health_score.desc(),
                )
            ).all()
            return [self.serialize(health, memory) for health, memory in rows]

    def archive(self, memory_id: str, *, actor: str) -> dict[str, Any]:
        with self.database.session() as session:
            memory = session.get(MemoryRow, memory_id)
            if memory is None:
                raise NotFoundError("memory was not found")
            self._assert_truth_safe(session, memory_id)
            health = session.get(MemoryHealthRow, memory_id)
            if health is None:
                health = MemoryHealthRow(
                    memory_id=memory_id,
                    temperature=MemoryTemperature.WARM,
                    health_score=0.5,
                    components_json={},
                    explanation="Archived before first scheduled health evaluation",
                    retrieval_count=0,
                    evaluated_at=datetime.now(UTC),
                )
                session.add(health)
            if health.temperature is not MemoryTemperature.ARCHIVED:
                components = dict(health.components_json)
                components["pre_archive_temperature"] = health.temperature.value
                health.components_json = components
            health.temperature = MemoryTemperature.ARCHIVED
            health.archived_at = datetime.now(UTC)
            health.evaluated_at = datetime.now(UTC)
            health.explanation = (
                "Reversibly archived; excluded from normal retrieval and truth views."
            )
            session.add(
                AuditEventRow(
                    action="health_archive",
                    entity_type="memory",
                    entity_id=memory_id,
                    actor=actor,
                    details={"reversible": True},
                )
            )
            session.flush()
            return self.serialize(health, memory)

    def restore(self, memory_id: str, *, actor: str) -> dict[str, Any]:
        with self.database.session() as session:
            memory = session.get(MemoryRow, memory_id)
            health = session.get(MemoryHealthRow, memory_id)
            if memory is None or health is None:
                raise NotFoundError("archived memory was not found")
            previous = str(health.components_json.get("pre_archive_temperature", "cold"))
            health.temperature = MemoryTemperature(previous)
            health.archived_at = None
            health.evaluated_at = datetime.now(UTC)
            health.explanation = "Restored from reversible archive; eligible for retrieval again."
            session.add(
                AuditEventRow(
                    action="health_restore",
                    entity_type="memory",
                    entity_id=memory_id,
                    actor=actor,
                    details={"temperature": health.temperature.value},
                )
            )
            session.flush()
            return self.serialize(health, memory)

    @staticmethod
    def record_retrieval(session: Any, memory_ids: list[str]) -> None:
        now = datetime.now(UTC)
        for memory_id in memory_ids:
            row = session.get(MemoryHealthRow, memory_id)
            if row is None:
                row = MemoryHealthRow(
                    memory_id=memory_id,
                    temperature=MemoryTemperature.WARM,
                    health_score=0.5,
                    components_json={},
                    explanation="Created from retrieval activity; full evaluation pending",
                    retrieval_count=0,
                    evaluated_at=now,
                )
                session.add(row)
            row.retrieval_count += 1
            row.last_retrieved_at = now
            if row.temperature is not MemoryTemperature.ARCHIVED:
                row.temperature = MemoryTemperature.HOT

    @staticmethod
    def _assert_truth_safe(session: Any, memory_id: str) -> None:
        accepted = list(
            session.scalars(
                select(ClaimVersionRow).where(
                    ClaimVersionRow.memory_id == memory_id,
                    ClaimVersionRow.transaction_to.is_(None),
                    ClaimVersionRow.status == ClaimStatus.ACCEPTED,
                )
            )
        )
        for version in accepted:
            alternatives = int(
                session.scalar(
                    select(func.count())
                    .select_from(ClaimVersionRow)
                    .outerjoin(
                        MemoryHealthRow,
                        MemoryHealthRow.memory_id == ClaimVersionRow.memory_id,
                    )
                    .where(
                        ClaimVersionRow.identity_id == version.identity_id,
                        ClaimVersionRow.transaction_to.is_(None),
                        ClaimVersionRow.status == ClaimStatus.ACCEPTED,
                        ClaimVersionRow.memory_id != memory_id,
                        (
                            (MemoryHealthRow.memory_id.is_(None))
                            | (MemoryHealthRow.temperature != MemoryTemperature.ARCHIVED)
                        ),
                    )
                )
                or 0
            )
            if alternatives == 0:
                raise InvalidTransitionError(
                    "cannot archive the sole accepted current-truth support",
                    details={"memory_id": memory_id, "identity_id": version.identity_id},
                )

    @staticmethod
    def _components(
        memory: MemoryRow,
        claims: list[ClaimRow],
        feedback: Counter[str],
        health: MemoryHealthRow,
        now: datetime,
    ) -> dict[str, float]:
        age_days = max(0.0, (now - _utc(memory.updated_at)).total_seconds() / 86400)
        recency = math.exp(-age_days / 90)
        usage = min(1.0, math.log1p(health.retrieval_count) / math.log(21))
        yes = feedback[FeedbackValue.YES.value]
        no = feedback[FeedbackValue.NO.value]
        feedback_score = (yes + 1) / (yes + no + 2)
        stale_states = {claim.stale_state for claim in claims}
        if ClaimStaleState.STALE in stale_states:
            freshness = 0.0
        elif ClaimStaleState.SUSPECT in stale_states:
            freshness = 0.3
        elif ClaimStaleState.FRESH in stale_states:
            freshness = 1.0
        else:
            freshness = 0.55
        return {
            "recency": round(recency, 6),
            "usage": round(usage, 6),
            "feedback": round(feedback_score, 6),
            "freshness": freshness,
            "importance": memory.importance,
            "confidence": memory.confidence,
        }

    @staticmethod
    def _explanation(
        temperature: MemoryTemperature,
        score: float,
        components: dict[str, float],
    ) -> str:
        strongest = max(components, key=components.get)  # type: ignore[arg-type]
        weakest = min(components, key=components.get)  # type: ignore[arg-type]
        return (
            f"{temperature.value.title()} at {score:.3f}; strongest signal is {strongest} "
            f"({components[strongest]:.2f}), weakest is {weakest} ({components[weakest]:.2f})."
        )

    @staticmethod
    def serialize(health: MemoryHealthRow, memory: MemoryRow) -> dict[str, Any]:
        return {
            "memory_id": memory.id,
            "title": memory.title,
            "memory_status": memory.status.value,
            "temperature": health.temperature.value,
            "health_score": health.health_score,
            "components": health.components_json,
            "explanation": health.explanation,
            "retrieval_count": health.retrieval_count,
            "last_retrieved_at": health.last_retrieved_at.isoformat()
            if health.last_retrieved_at
            else None,
            "archived_at": health.archived_at.isoformat() if health.archived_at else None,
            "evaluated_at": health.evaluated_at.isoformat(),
        }
