from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from memoryos.claims.truth import TruthMaintenanceService
from memoryos.config import MemoryOSSettings
from memoryos.consolidation import ConsolidationService
from memoryos.context import TaskAwareContextCompiler
from memoryos.db.models import (
    AuditEventRow,
    ClaimEvidenceRow,
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    ConsolidationCandidateRow,
    MemoryHealthRow,
    MemoryRow,
    PossibleConflictRow,
    RelationRow,
    RetrievalRunRow,
    SourceAnchorRow,
    SourceRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import (
    ConflictStrategy,
    ConsolidateRequest,
    ContextRequest,
    CreatedBy,
    CurrentTruthRequest,
    FeedbackCreate,
    MemoryCreate,
    MemoryStatus,
    MemoryTemperature,
    MemoryType,
    MemoryUpdate,
    MemoryView,
    PossibleConflictStatus,
    RefreshRequest,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.errors import (
    ConflictDetectedError,
    InvalidTransitionError,
    NotFoundError,
    ProviderError,
)
from memoryos.feedback import FeedbackService
from memoryos.freshness import SourceAnchorService
from memoryos.health import MemoryHealthService
from memoryos.providers.openai_compatible import (
    OpenAICompatibleConsolidationJudge,
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleRelationshipJudge,
    OpenAICompatibleReranker,
)
from memoryos.retrieval.search import RetrievalEngine
from memoryos.retrieval_v2 import (
    RetrievalPipeline,
    RetrievalRoutingShadowProfile,
    RRFChannelShadowProfile,
    ShadowRetrievalProfile,
)
from memoryos.security.redaction import redact_secrets

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalized_key(memory: MemoryRow) -> str:
    raw = memory.key or memory.subject or memory.title
    return re.sub(r"[^a-z0-9.]+", ".", raw.lower()).strip(".")


class MemoryService:
    def __init__(
        self,
        database: Database,
        settings: MemoryOSSettings,
        *,
        retrieval_scoring_profile: ShadowRetrievalProfile | None = None,
        retrieval_rrf_channel_profile: RRFChannelShadowProfile | None = None,
        retrieval_routing_profile: RetrievalRoutingShadowProfile | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        shadow_profile_count = sum(
            profile is not None
            for profile in (
                retrieval_scoring_profile,
                retrieval_rrf_channel_profile,
                retrieval_routing_profile,
            )
        )
        if shadow_profile_count > 1:
            raise ValueError("only one retrieval shadow profile may be active")
        if retrieval_rrf_channel_profile is not None:
            if not settings.embedding_base_url or not settings.embedding_model:
                raise ValueError("RRF channel shadow requires an embedding provider")
            expected_prefix = f"fastembed:{settings.embedding_model}@"
            if not retrieval_rrf_channel_profile.source_vector_channel_id.startswith(
                expected_prefix
            ):
                raise ValueError("embedding model does not match the RRF channel shadow source")
        embedding_provider = None
        if settings.embedding_base_url and settings.embedding_model:
            embedding_provider = OpenAICompatibleEmbeddingProvider(
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
                api_key=settings.embedding_api_key,
                timeout=settings.provider_timeout_seconds,
                max_input_chars=settings.provider_max_input_chars,
            )
        reranker = None
        if settings.extractor_base_url and settings.reranker_model:
            reranker = OpenAICompatibleReranker(
                base_url=settings.extractor_base_url,
                model=settings.reranker_model,
                api_key=settings.extractor_api_key,
                timeout=settings.provider_timeout_seconds,
                max_input_chars=settings.provider_max_input_chars,
            )
        relationship_judge = None
        if settings.extractor_base_url and settings.relationship_model:
            relationship_judge = OpenAICompatibleRelationshipJudge(
                base_url=settings.extractor_base_url,
                model=settings.relationship_model,
                api_key=settings.extractor_api_key,
                timeout=settings.provider_timeout_seconds,
                max_input_chars=settings.provider_max_input_chars,
            )
        consolidation_judge = None
        if settings.extractor_base_url and settings.consolidation_model:
            consolidation_judge = OpenAICompatibleConsolidationJudge(
                base_url=settings.extractor_base_url,
                model=settings.consolidation_model,
                api_key=settings.extractor_api_key,
                timeout=settings.provider_timeout_seconds,
                max_input_chars=settings.provider_max_input_chars,
            )
        self.retrieval = RetrievalEngine(database, embedding_provider)
        self.retrieval_v2 = RetrievalPipeline(
            database,
            self.retrieval,
            reranker,
            scoring_profile=retrieval_scoring_profile,
            rrf_channel_profile=retrieval_rrf_channel_profile,
            routing_profile=retrieval_routing_profile,
        )
        self.context_builder = TaskAwareContextCompiler(self.retrieval_v2)
        self.truth = TruthMaintenanceService(relationship_judge)
        self.anchors = SourceAnchorService(database)
        self.consolidation = ConsolidationService(database, consolidation_judge)
        self.feedback_service = FeedbackService(database)
        self.health_service = MemoryHealthService(database)

    def close(self) -> None:
        self.retrieval.close()

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
                expiry_reason = "valid_to" if valid_to_expired else "ttl"
                self.truth.expire_claims(
                    session,
                    memory.id,
                    reason=f"Memory expired by {expiry_reason}",
                    at=now,
                )
                self._audit(
                    session,
                    "expire",
                    memory.id,
                    actor="system",
                    details={"from": previous, "reason": expiry_reason},
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
            self.truth.ensure_claims(
                session,
                memory,
                payload.claim_candidates if payload.claim_candidates else None,
            )
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
            if (
                memory.valid_from is not None
                and memory.valid_to is not None
                and _utc(memory.valid_to) <= _utc(memory.valid_from)
            ):
                raise ValueError("valid_to must be later than valid_from")
            if {"title", "content", "category", "subject", "key"}.intersection(changes):
                source = SourceRow(
                    source_type=SourceType.MANUAL,
                    source_ref=f"{actor}:edit:{memory.id}",
                    captured_at=datetime.now(UTC),
                    excerpt=memory.content,
                    content_hash=hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
                    metadata_json={"edited_candidate": True},
                )
                memory.sources.insert(0, source)
                self.truth.reset_claims(session, memory)
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
        exact = [row for row in active_rows if _normalized_key(row) == semantic_key]
        semantic = self.truth.find_semantic_conflict_memories(session, candidate)
        return list({row.id: row for row in [*exact, *semantic]}.values())

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
                self.truth.reject_claims(
                    session,
                    memory.id,
                    actor=actor,
                    reason=rationale or "Rejected during conflict resolution",
                )
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
                self.truth.activate_claims(
                    session,
                    memory,
                    strategy=strategy,
                    conflicts=conflicts,
                    rationale=rationale,
                    actor=actor,
                )
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
            self.truth.reject_claims(session, memory.id, actor=actor)
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
            self.truth.forget_claims(session, memory.id, actor=actor)
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
        if request.as_of_valid_time is None and request.as_known_at is None:
            with self.database.session() as session:
                self._expire_due(session)
        return self.retrieval_v2.search(request)

    def vector_status(self) -> list[dict[str, Any]]:
        return self.retrieval.vector_status()

    def rebuild_vector_index(self) -> dict[str, Any]:
        return self.retrieval.rebuild_ann_index()

    def memory_health(
        self, *, temperature: MemoryTemperature | None = None
    ) -> list[dict[str, Any]]:
        return self.health_service.items(temperature=temperature)

    def evaluate_memory_health(self) -> dict[str, Any]:
        return self.health_service.evaluate()

    def archive_memory(self, memory_id: str, *, actor: str = "manual") -> dict[str, Any]:
        return self.health_service.archive(memory_id, actor=actor)

    def restore_archived_memory(self, memory_id: str, *, actor: str = "manual") -> dict[str, Any]:
        return self.health_service.restore(memory_id, actor=actor)

    def distill_memories(
        self,
        memory_ids: list[str],
        *,
        title: str | None = None,
        actor: str = "manual",
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(memory_ids))
        if len(unique_ids) < 2:
            raise ValueError("distillation requires at least two cold or archived memories")
        with self.database.session() as session:
            memories = list(session.scalars(select(MemoryRow).where(MemoryRow.id.in_(unique_ids))))
            if len(memories) != len(unique_ids):
                raise NotFoundError("one or more distillation memories were not found")
            health_by_id = {item["memory_id"]: item for item in self.health_service.items()}
            disallowed = [
                memory.id
                for memory in memories
                if health_by_id.get(memory.id, {}).get("temperature")
                not in {MemoryTemperature.COLD.value, MemoryTemperature.ARCHIVED.value}
            ]
            if disallowed:
                raise InvalidTransitionError(
                    "distillation is limited to cold or archived memories",
                    details={"memory_ids": disallowed},
                )
            scopes = {(memory.scope_type, memory.scope_key) for memory in memories}
            if len(scopes) != 1:
                raise ValueError("distillation memories must share a scope")
            first = memories[0]
            content = "\n\n".join(
                f"[{memory.id}] {memory.title}: {memory.content}" for memory in memories
            )
        digest = hashlib.sha256("|".join(sorted(unique_ids)).encode("utf-8")).hexdigest()[:16]
        proposal = self.propose(
            MemoryCreate(
                scope_type=first.scope_type,
                scope_key=first.scope_key,
                memory_type=MemoryType.SEMANTIC,
                category="distillation",
                title=title or f"Distillation candidate {digest}",
                content=content[:20000],
                created_by=CreatedBy.AGENT,
                metadata={
                    "distilled_from": unique_ids,
                    "activation": "human_confirmation_required",
                    "mode": "grounded-extractive",
                },
                source=SourceCreate(
                    source_type=SourceType.AGENT,
                    source_ref=f"memory-health:distillation:{digest}",
                    excerpt=content[:10000],
                    metadata={"supporting_memory_ids": unique_ids},
                ),
            ),
            actor=actor,
        )
        return {"candidate": proposal, "supporting_memory_ids": unique_ids}

    def context(self, request: ContextRequest) -> dict[str, Any]:
        if request.as_of_valid_time is None and request.as_known_at is None:
            with self.database.session() as session:
                self._expire_due(session)
        return self.context_builder.build(request)

    def current_truth(self, request: CurrentTruthRequest) -> dict[str, Any]:
        with self.database.session() as session:
            self._expire_due(session)
            return self.truth.current_truth(session, request)

    def create_source_anchor(
        self,
        *,
        memory_id: str,
        repository_path: str,
        path: str,
        symbol_fqn: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> dict[str, Any]:
        return self.anchors.create(
            memory_id=memory_id,
            repository_path=repository_path,
            path=path,
            symbol_fqn=symbol_fqn,
            line_start=line_start,
            line_end=line_end,
        )

    def refresh_memory(self, request: RefreshRequest) -> dict[str, Any]:
        result = self.anchors.refresh(
            memory_id=request.memory_id,
            repository_path=request.repository_path,
        )
        suggestion = result.get("replacement_candidate")
        if request.create_replacement_candidate and isinstance(suggestion, dict):
            evidence = suggestion.get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                original = self.get(request.memory_id)
                replacement = self.propose(
                    MemoryCreate(
                        scope_type=original["scope_type"],
                        scope_key=original["scope_key"],
                        memory_type=original["memory_type"],
                        category=original["category"],
                        subject=original["subject"],
                        key=original["key"],
                        title=f"Refresh: {original['title']}",
                        content=original["content"],
                        confidence=max(0.0, float(original["confidence"]) * 0.8),
                        importance=float(original["importance"]),
                        created_by=CreatedBy.AGENT,
                        metadata={
                            "refresh_of": request.memory_id,
                            "freshness": result["freshness"],
                        },
                        source=SourceCreate(
                            source_type=SourceType.FILE_REFERENCE,
                            source_ref=f"git-refresh:{request.repository_path}",
                            excerpt=evidence,
                        ),
                    ),
                    actor="memory_refresh",
                )
                result["replacement_candidate"] = replacement
        return result

    def consolidate(self, request: ConsolidateRequest) -> dict[str, Any]:
        return self.consolidation.propose(request)

    def feedback(self, payload: FeedbackCreate) -> dict[str, Any]:
        return self.feedback_service.submit(payload)

    def debug_context(self, request: ContextRequest) -> dict[str, Any]:
        return self.context(request)

    def claim_graph(self, request: CurrentTruthRequest) -> dict[str, Any]:
        with self.database.session() as session:
            truth = self.truth.current_truth(session, request)
            claim_ids = {
                item["id"]
                for group in truth["truths"]
                for item in [*group["accepted_claims"], *group["conflicting_claims"]]
            }
            claims = [
                self.truth.serialize_claim(session, claim)
                for claim in session.scalars(
                    select(ClaimRow).where(ClaimRow.id.in_(claim_ids or {""}))
                )
            ]
            relations = list(
                session.scalars(
                    select(ClaimRelationRow).where(
                        or_(
                            ClaimRelationRow.from_claim_id.in_(claim_ids or {""}),
                            ClaimRelationRow.to_claim_id.in_(claim_ids or {""}),
                        )
                    )
                )
            )
            return {
                "state": truth["state"],
                "nodes": claims,
                "edges": [
                    {
                        "id": row.id,
                        "from": row.from_claim_id,
                        "to": row.to_claim_id,
                        "type": row.relation_type.value,
                        "confidence": row.confidence,
                        "method": row.method.value,
                        "explanation": row.explanation,
                    }
                    for row in relations
                ],
            }

    def freshness(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(SourceAnchorRow, ClaimRow, MemoryRow)
                .join(ClaimEvidenceRow, ClaimEvidenceRow.source_anchor_id == SourceAnchorRow.id)
                .join(ClaimRow, ClaimRow.id == ClaimEvidenceRow.claim_id)
                .join(MemoryRow, MemoryRow.id == ClaimRow.memory_id)
                .order_by(SourceAnchorRow.created_at.desc())
                .limit(limit)
            ).all()
            seen: set[str] = set()
            items = []
            for anchor, claim, memory in rows:
                if anchor.id in seen:
                    continue
                seen.add(anchor.id)
                items.append(
                    {
                        "anchor_id": anchor.id,
                        "memory_id": memory.id,
                        "memory_title": memory.title,
                        "claim_id": claim.id,
                        "path": anchor.path,
                        "symbol_fqn": anchor.symbol_fqn,
                        "freshness": anchor.freshness_state.value,
                        "commit_sha": anchor.commit_sha,
                        "cached_head": anchor.cached_head,
                        "checked_at": anchor.checked_at.isoformat() if anchor.checked_at else None,
                    }
                )
            return items

    def consolidation_inbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ConsolidationCandidateRow)
                    .order_by(ConsolidationCandidateRow.created_at.desc())
                    .limit(limit)
                )
            )
            return [
                {
                    "id": row.id,
                    "scope_type": row.scope_type.value,
                    "scope_key": row.scope_key,
                    "subject_entity_id": row.subject_entity_id,
                    "predicate": row.predicate,
                    "proposal": row.proposal_json,
                    "status": row.status,
                    "source_memory_ids": row.source_memory_ids,
                    "counterevidence": row.counterevidence_json,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def retrieval_run(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(RetrievalRunRow, run_id)
            if row is None:
                raise NotFoundError("retrieval run was not found")
            return {
                "id": row.id,
                "query": row.query,
                "task": row.task,
                "scope": row.scope_json,
                "selected_memory_ids": row.selected_memory_ids,
                "candidate_features": row.candidate_features,
                "context_manifest": row.context_manifest,
                "config_hash": row.config_hash,
                "created_at": row.created_at.isoformat(),
            }

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

    def possible_conflicts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PossibleConflictRow)
                    .order_by(PossibleConflictRow.created_at.desc())
                    .limit(limit)
                )
            )
            return [self._serialize_possible_conflict(row) for row in rows]

    def resolve_possible_conflict(
        self,
        conflict_id: str,
        *,
        confirmed: bool,
        actor: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(PossibleConflictRow, conflict_id)
            if row is None:
                raise NotFoundError("possible conflict was not found")
            if row.resolved_at is not None:
                raise InvalidTransitionError("possible conflict was already manually resolved")
            row.status = (
                PossibleConflictStatus.CONFIRMED if confirmed else PossibleConflictStatus.DISMISSED
            )
            row.resolved_at = datetime.now(UTC)
            row.resolved_by = actor
            result = dict(row.model_result_json)
            result["manual_resolution"] = {
                "confirmed": confirmed,
                "rationale": rationale,
            }
            row.model_result_json = result
            self.truth.apply_manual_conflict_resolution(
                session,
                left_claim_id=row.left_claim_id,
                right_claim_id=row.right_claim_id,
                confirmed=confirmed,
                actor=actor,
                rationale=rationale,
            )
            session.add(
                AuditEventRow(
                    action="possible_conflict_resolved",
                    entity_type="possible_conflict",
                    entity_id=row.id,
                    actor=actor,
                    details={"confirmed": confirmed, "rationale": rationale},
                )
            )
            session.flush()
            return self._serialize_possible_conflict(row)

    @staticmethod
    def _serialize_possible_conflict(row: PossibleConflictRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "left_claim_id": row.left_claim_id,
            "right_claim_id": row.right_claim_id,
            "status": row.status.value,
            "deterministic_relationship": row.deterministic_relationship,
            "deterministic_confidence": row.deterministic_confidence,
            "reason": row.reason,
            "model_result": row.model_result_json,
            "provider_fingerprint": row.provider_fingerprint,
            "prompt_version": row.prompt_version,
            "evidence_hash": row.evidence_hash,
            "created_at": row.created_at.isoformat(),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            "resolved_by": row.resolved_by,
        }

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
            claims = list(
                session.scalars(
                    select(ClaimRow)
                    .where(ClaimRow.memory_id == memory.id)
                    .order_by(ClaimRow.recorded_at.asc())
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
                "claims": [self.truth.serialize_claim(session, claim) for claim in claims],
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
            claim_count = int(session.scalar(select(func.count()).select_from(ClaimRow)) or 0)
            claim_version_count = int(
                session.scalar(select(func.count()).select_from(ClaimVersionRow)) or 0
            )
            possible_conflict_count = int(
                session.scalar(select(func.count()).select_from(PossibleConflictRow)) or 0
            )
            health_counts = {
                temperature.value: int(
                    session.scalar(
                        select(func.count())
                        .select_from(MemoryHealthRow)
                        .where(MemoryHealthRow.temperature == temperature)
                    )
                    or 0
                )
                for temperature in MemoryTemperature
            }
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
                "version": "2.1.0",
                "database": str(self.settings.database_path),
                "schema_version": self.database.schema_version(),
                "counts": counts,
                "sources": source_count,
                "claims": claim_count,
                "claim_versions": claim_version_count,
                "provenance_rate": provenance_rate,
                "conflicts": len(self.conflicts()),
                "possible_conflicts": possible_conflict_count,
                "memory_health": health_counts,
                "vector_index": self.retrieval.vector_status(),
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
