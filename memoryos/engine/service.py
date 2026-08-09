from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from memoryos.config import MemoryOSSettings
from memoryos.db.models import (
    AuditEventRow,
    MemoryRow,
    RelationRow,
    SourceRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ConflictStrategy,
    ContextRequest,
    MemoryCreate,
    MemoryStatus,
    MemoryUpdate,
    MemoryView,
    SearchRequest,
)
from memoryos.errors import (
    ConflictDetectedError,
    InvalidTransitionError,
    NotFoundError,
    ProviderError,
)
from memoryos.providers.openai_compatible import OpenAICompatibleEmbeddingProvider
from memoryos.retrieval.context import ContextBuilder
from memoryos.retrieval.search import RetrievalEngine
from memoryos.security.redaction import redact_secrets

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalized_key(memory: MemoryRow) -> str:
    raw = memory.key or memory.subject or memory.title
    return re.sub(r"[^a-z0-9.]+", ".", raw.lower()).strip(".")


class MemoryService:
    def __init__(self, database: Database, settings: MemoryOSSettings) -> None:
        self.database = database
        self.settings = settings
        embedding_provider = None
        if settings.embedding_base_url and settings.embedding_model:
            embedding_provider = OpenAICompatibleEmbeddingProvider(
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
                api_key=settings.embedding_api_key,
            )
        self.retrieval = RetrievalEngine(database, embedding_provider)
        self.context_builder = ContextBuilder(self.retrieval)

    def _index_memory_safely(self, memory_id: str) -> None:
        try:
            self.retrieval.index_memory(memory_id)
        except ProviderError as exc:
            logger.warning("Embedding indexing failed for memory %s: %s", memory_id, exc)

    def _audit(
        self,
        session: Session,
        action: str,
        memory_id: str,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEventRow(
                action=action,
                entity_type="memory",
                entity_id=memory_id,
                actor=actor,
                details=details or {},
            )
        )

    def _get(self, session: Session, memory_id: str) -> MemoryRow:
        memory = session.get(MemoryRow, memory_id)
        if memory is None:
            raise NotFoundError(f"memory {memory_id} was not found")
        return memory

    def _expire_due(self, session: Session) -> int:
        now = datetime.now(UTC)
        candidates = list(
            session.scalars(
                select(MemoryRow).where(
                    MemoryRow.status.in_([MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE]),
                    or_(MemoryRow.valid_to.is_not(None), MemoryRow.ttl_seconds.is_not(None)),
                )
            )
        )
        expired = 0
        for memory in candidates:
            valid_to_expired = memory.valid_to is not None and _utc(memory.valid_to) <= now
            ttl_expired = memory.ttl_seconds is not None and (
                _utc(memory.created_at) + timedelta(seconds=memory.ttl_seconds) <= now
            )
            if valid_to_expired or ttl_expired:
                previous = memory.status.value
                memory.status = MemoryStatus.EXPIRED
                self._audit(
                    session,
                    "expire",
                    memory.id,
                    actor="system",
                    details={"from": previous, "reason": "valid_to" if valid_to_expired else "ttl"},
                )
                expired += 1
        return expired

    def propose(self, payload: MemoryCreate, *, actor: str = "agent") -> dict[str, Any]:
        content_redaction = redact_secrets(payload.content)
        excerpt_redaction = redact_secrets(
            payload.source.excerpt, max_length=self.settings.source_excerpt_limit
        )
        status = MemoryStatus.ACTIVE if payload.activate_immediately else MemoryStatus.CANDIDATE
        metadata = dict(payload.metadata)
        detected = sorted(set(content_redaction.detected_types + excerpt_redaction.detected_types))
        if detected:
            metadata["secret_redaction"] = {"detected": detected, "applied": True}

        with self.database.session() as session:
            memory = MemoryRow(
                scope_type=payload.scope_type,
                scope_key=payload.scope_key,
                memory_type=payload.memory_type,
                category=payload.category.strip().lower(),
                subject=payload.subject.strip() if payload.subject else None,
                key=payload.key.strip().lower() if payload.key else None,
                title=payload.title.strip(),
                content=content_redaction.text.strip(),
                status=status,
                confidence=payload.confidence,
                importance=payload.importance,
                valid_from=payload.valid_from,
                valid_to=payload.valid_to,
                ttl_seconds=payload.ttl_seconds,
                created_by=payload.created_by,
                sensitivity=payload.sensitivity,
                metadata_json=metadata,
            )
            source = SourceRow(
                source_type=payload.source.source_type,
                source_ref=payload.source.source_ref,
                captured_at=payload.source.captured_at,
                excerpt=excerpt_redaction.text,
                content_hash=hashlib.sha256(excerpt_redaction.text.encode("utf-8")).hexdigest(),
                metadata_json={
                    **payload.source.metadata,
                    "redaction": {
                        "detected": list(excerpt_redaction.detected_types),
                        "applied": excerpt_redaction.was_redacted,
                    },
                },
            )
            memory.sources.append(source)
            session.add(memory)
            session.flush()
            if status is MemoryStatus.ACTIVE:
                conflicts = self._find_conflicts(session, memory)
                if conflicts:
                    raise ConflictDetectedError(
                        "manual activation conflicts with active memory; save a candidate instead",
                        details={
                            "candidate_id": memory.id,
                            "conflict_ids": [row.id for row in conflicts],
                        },
                    )
            self._audit(
                session,
                "create_active" if status is MemoryStatus.ACTIVE else "propose",
                memory.id,
                actor=actor,
                details={"status": status.value, "source_id": source.id},
            )
            session.flush()
            result = self._serialize_memory(memory)
        if status is MemoryStatus.ACTIVE:
            self._index_memory_safely(str(result["id"]))
        return result

    def update(
        self, memory_id: str, payload: MemoryUpdate, *, actor: str = "manual"
    ) -> dict[str, Any]:
        with self.database.session() as session:
            memory = self._get(session, memory_id)
            if memory.status is not MemoryStatus.CANDIDATE:
                raise InvalidTransitionError(
                    "only candidate memories can be edited in place; "
                    "create a candidate revision instead",
                    details={"status": memory.status.value},
                )
            changes = payload.model_dump(exclude_unset=True)
            if "content" in changes:
                redacted = redact_secrets(str(changes["content"]))
                changes["content"] = redacted.text
                if redacted.was_redacted:
                    metadata = dict(memory.metadata_json)
                    metadata["secret_redaction"] = {
                        "detected": list(redacted.detected_types),
                        "applied": True,
                    }
                    changes["metadata_json"] = metadata
            if "metadata" in changes:
                changes["metadata_json"] = changes.pop("metadata")
            for field, value in changes.items():
                setattr(memory, field, value)
            memory.updated_at = datetime.now(UTC)
            self._audit(
                session, "edit", memory.id, actor=actor, details={"fields": sorted(changes)}
            )
            session.flush()
            return self._serialize_memory(memory)

    def _find_conflicts(self, session: Session, candidate: MemoryRow) -> list[MemoryRow]:
        semantic_key = _normalized_key(candidate)
        active_rows = list(
            session.scalars(
                select(MemoryRow).where(
                    MemoryRow.scope_type == candidate.scope_type,
                    MemoryRow.scope_key == candidate.scope_key,
                    MemoryRow.status == MemoryStatus.ACTIVE,
                    MemoryRow.id != candidate.id,
                )
            )
        )
        return [row for row in active_rows if _normalized_key(row) == semantic_key]

    def confirm(
        self,
        memory_id: str,
        *,
        strategy: ConflictStrategy | None = None,
        actor: str = "manual",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
            memory = self._get(session, memory_id)
            if memory.status is not MemoryStatus.CANDIDATE:
                raise InvalidTransitionError(
                    "only candidate memories can be confirmed",
                    details={"status": memory.status.value},
                )
            conflicts = self._find_conflicts(session, memory)
            if conflicts and strategy is None:
                raise ConflictDetectedError(
                    "candidate conflicts with active memory; choose a resolution strategy",
                    details={
                        "candidate_id": memory.id,
                        "conflict_ids": [row.id for row in conflicts],
                    },
                )
            if strategy is ConflictStrategy.REJECT:
                memory.status = MemoryStatus.REJECTED
                self._audit(
                    session,
                    "reject",
                    memory.id,
                    actor=actor,
                    details={"conflict_ids": [row.id for row in conflicts], "rationale": rationale},
                )
                session.flush()
            else:
                if conflicts and strategy is ConflictStrategy.SUPERSEDE:
                    memory.supersedes_id = conflicts[0].id
                    for current in conflicts:
                        current.status = MemoryStatus.SUPERSEDED
                        session.add(
                            RelationRow(
                                from_memory_id=memory.id,
                                to_memory_id=current.id,
                                relation_type="supersedes",
                                metadata_json={"rationale": rationale},
                            )
                        )
                        self._audit(
                            session,
                            "supersede",
                            current.id,
                            actor=actor,
                            details={"superseded_by": memory.id, "rationale": rationale},
                        )
                elif conflicts and strategy is ConflictStrategy.KEEP_BOTH:
                    for current in conflicts:
                        session.add(
                            RelationRow(
                                from_memory_id=memory.id,
                                to_memory_id=current.id,
                                relation_type="alternative_to",
                                metadata_json={"rationale": rationale},
                            )
                        )

                memory.status = MemoryStatus.ACTIVE
                memory.updated_at = datetime.now(UTC)
                self._audit(
                    session,
                    "confirm",
                    memory.id,
                    actor=actor,
                    details={
                        "strategy": strategy.value if strategy else "activate",
                        "conflict_ids": [row.id for row in conflicts],
                        "rationale": rationale,
                    },
                )
            session.flush()
            result = self._serialize_memory(memory)
        if result["status"] == MemoryStatus.ACTIVE.value:
            self._index_memory_safely(str(result["id"]))
        return result

    def reject(self, memory_id: str, *, actor: str = "manual") -> dict[str, Any]:
        with self.database.session() as session:
            memory = self._get(session, memory_id)
            if memory.status is not MemoryStatus.CANDIDATE:
                raise InvalidTransitionError("only candidate memories can be rejected")
            memory.status = MemoryStatus.REJECTED
            self._audit(session, "reject", memory.id, actor=actor)
            session.flush()
            return self._serialize_memory(memory)

    def forget(self, memory_id: str, *, actor: str = "manual") -> dict[str, Any]:
        with self.database.session() as session:
            memory = self._get(session, memory_id)
            if memory.status in {MemoryStatus.FORGOTTEN, MemoryStatus.REJECTED}:
                raise InvalidTransitionError(
                    "memory cannot transition to forgotten",
                    details={"status": memory.status.value},
                )
            previous = memory.status
            memory.status = MemoryStatus.FORGOTTEN
            self._audit(
                session,
                "forget",
                memory.id,
                actor=actor,
                details={"from": previous.value},
            )
            session.flush()
            return self._serialize_memory(memory)

    def get(self, memory_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
            return self._serialize_memory(self._get(session, memory_id))

    def search(self, request: SearchRequest) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
        return self.retrieval.search(request)

    def context(self, request: ContextRequest) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
        return self.context_builder.build(request)

    def conflicts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            self._expire_due(session)
            candidates = list(
                session.scalars(
                    select(MemoryRow)
                    .where(MemoryRow.status == MemoryStatus.CANDIDATE)
                    .order_by(MemoryRow.created_at.desc())
                    .limit(limit)
                )
            )
            results: list[dict[str, Any]] = []
            for candidate in candidates:
                conflicts = self._find_conflicts(session, candidate)
                if conflicts:
                    results.append(
                        {
                            "candidate": self._serialize_memory(candidate),
                            "current": [self._serialize_memory(row) for row in conflicts],
                            "semantic_key": _normalized_key(candidate),
                            "status": "needs_review",
                        }
                    )
            return results

    def history(
        self, *, memory_id: str | None = None, key: str | None = None
    ) -> list[dict[str, Any]]:
        if memory_id is None and key is None:
            raise ValueError("memory_id or key is required")
        with self.database.session() as session:
            target = self._get(session, memory_id) if memory_id else None
            normalized = _normalized_key(target) if target else str(key).strip().lower()
            rows = list(session.scalars(select(MemoryRow).order_by(MemoryRow.created_at.asc())))
            if target:
                rows = [
                    row
                    for row in rows
                    if row.scope_type == target.scope_type
                    and row.scope_key == target.scope_key
                    and _normalized_key(row) == normalized
                ]
            else:
                rows = [row for row in rows if _normalized_key(row) == normalized]
            return [self._serialize_memory(row) for row in rows]

    def explain(self, memory_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            memory = self._get(session, memory_id)
            sources = [
                {
                    "id": source.id,
                    "source_type": source.source_type.value,
                    "source_ref": source.source_ref,
                    "captured_at": _utc(source.captured_at).isoformat(),
                    "excerpt": source.excerpt,
                    "content_hash": source.content_hash,
                    "metadata": source.metadata_json,
                }
                for source in memory.sources
            ]
            relations = list(
                session.scalars(
                    select(RelationRow).where(
                        or_(
                            RelationRow.from_memory_id == memory.id,
                            RelationRow.to_memory_id == memory.id,
                        )
                    )
                )
            )
            audit = list(
                session.scalars(
                    select(AuditEventRow)
                    .where(AuditEventRow.entity_id == memory.id)
                    .order_by(AuditEventRow.timestamp.asc())
                )
            )
            return {
                "memory": self._serialize_memory(memory),
                "sources": sources,
                "relations": [
                    {
                        "id": relation.id,
                        "from_memory_id": relation.from_memory_id,
                        "to_memory_id": relation.to_memory_id,
                        "relation_type": relation.relation_type,
                        "metadata": relation.metadata_json,
                    }
                    for relation in relations
                ],
                "audit": [self._serialize_audit(event) for event in audit],
                "reason": (
                    "This memory is known because it has explicit, hashed provenance "
                    "and a complete audit trail."
                ),
            }

    def timeline(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            events = list(
                session.scalars(
                    select(AuditEventRow).order_by(AuditEventRow.timestamp.desc()).limit(limit)
                )
            )
            return [self._serialize_audit(event) for event in events]

    def status(self) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
            counts = {
                status.value: int(
                    session.scalar(
                        select(func.count())
                        .select_from(MemoryRow)
                        .where(MemoryRow.status == status)
                    )
                    or 0
                )
                for status in MemoryStatus
            }
            source_count = int(session.scalar(select(func.count()).select_from(SourceRow)) or 0)
            active_count = counts[MemoryStatus.ACTIVE.value]
            active_with_source = int(
                session.scalar(
                    select(func.count(func.distinct(MemoryRow.id)))
                    .select_from(MemoryRow)
                    .join(MemoryRow.sources)
                    .where(MemoryRow.status == MemoryStatus.ACTIVE)
                )
                or 0
            )
            provenance_rate = active_with_source / active_count if active_count else 1.0
            return {
                "version": "1.0.0",
                "database": str(self.settings.database_path),
                "schema_version": self.database.schema_version(),
                "counts": counts,
                "sources": source_count,
                "provenance_rate": provenance_rate,
                "conflicts": len(self.conflicts()),
                "embedding_provider": "configured"
                if self.settings.embedding_base_url and self.settings.embedding_model
                else "disabled",
                "mode": "offline" if not self.settings.extractor_base_url else "provider-optional",
            }

    @staticmethod
    def _serialize_memory(memory: MemoryRow) -> dict[str, Any]:
        return MemoryView(
            id=memory.id,
            scope_type=memory.scope_type,
            scope_key=memory.scope_key,
            memory_type=memory.memory_type,
            category=memory.category,
            subject=memory.subject,
            key=memory.key,
            title=memory.title,
            content=memory.content,
            status=memory.status,
            confidence=memory.confidence,
            importance=memory.importance,
            valid_from=memory.valid_from,
            valid_to=memory.valid_to,
            ttl_seconds=memory.ttl_seconds,
            supersedes_id=memory.supersedes_id,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            created_by=memory.created_by,
            sensitivity=memory.sensitivity,
            metadata_json=memory.metadata_json,
        ).model_dump(mode="json", by_alias=True)

    @staticmethod
    def _serialize_audit(event: AuditEventRow) -> dict[str, Any]:
        return {
            "id": event.id,
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "actor": event.actor,
            "timestamp": _utc(event.timestamp).isoformat(),
            "details": event.details,
        }
