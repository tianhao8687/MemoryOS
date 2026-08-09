from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from memoryos.claims.canonicalize import (
    canonical_claim_key,
    canonical_object,
    extract_claim_candidates,
    validate_claim_candidates,
)
from memoryos.claims.predicates import compare_claim_values, is_single_valued
from memoryos.db.models import (
    AuditEventRow,
    ClaimEvidenceRow,
    ClaimRelationRow,
    ClaimRow,
    EntityRow,
    MemoryRow,
    SourceAnchorRow,
    SourceRow,
)
from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimObjectKind,
    ClaimRelationType,
    ClaimStaleState,
    ClaimStatus,
    ConflictStrategy,
    CurrentTruthRequest,
    MemoryStatus,
    RelationMethod,
    TruthState,
)
from memoryos.entities import EntityResolver, normalize_entity_name
from memoryos.temporal import as_of, is_known_at


def _now() -> datetime:
    return datetime.now(UTC)


class TruthMaintenanceService:
    """Claim lifecycle, semantic relationships, and bitemporal current truth."""

    def __init__(self) -> None:
        self.entities = EntityResolver()

    def ensure_claims(
        self,
        session: Session,
        memory: MemoryRow,
        candidates: list[ClaimCandidate] | None = None,
    ) -> list[ClaimRow]:
        existing = list(session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory.id)))
        if existing:
            return existing
        source = memory.sources[0] if memory.sources else None
        evidence_text = source.excerpt if source is not None else memory.content
        proposed = candidates or extract_claim_candidates(
            evidence_text,
            title=memory.title,
            category=memory.category,
            key=memory.key,
            subject=memory.subject,
        )
        valid, rejected = validate_claim_candidates(proposed, evidence_text)
        if rejected:
            session.add(
                AuditEventRow(
                    action="claim_extraction_rejected",
                    entity_type="memory",
                    entity_id=memory.id,
                    actor="system",
                    details={"rejected": rejected},
                )
            )
        status = (
            ClaimStatus.ACCEPTED if memory.status is MemoryStatus.ACTIVE else ClaimStatus.CANDIDATE
        )
        claims: list[ClaimRow] = []
        for candidate in valid:
            subject = self.entities.resolve(
                session,
                scope_type=memory.scope_type,
                scope_key=memory.scope_key,
                entity_type=candidate.subject_type,
                name=candidate.subject_hint,
            )
            object_entity = None
            object_value = candidate.object_value
            if candidate.object_kind is ClaimObjectKind.ENTITY:
                assert candidate.object_entity_hint is not None
                object_entity = self.entities.resolve(
                    session,
                    scope_type=memory.scope_type,
                    scope_key=memory.scope_key,
                    entity_type=candidate.object_entity_type or candidate.subject_type,
                    name=candidate.object_entity_hint,
                )
                object_value = object_entity.normalized_name
            claim = ClaimRow(
                memory_id=memory.id,
                subject_entity_id=subject.id,
                predicate=candidate.predicate,
                object_kind=candidate.object_kind,
                object_entity_id=object_entity.id if object_entity else None,
                object_value=object_value,
                polarity=candidate.polarity,
                modality=candidate.modality,
                qualifiers_json=candidate.qualifiers,
                canonical_key=canonical_claim_key(
                    subject.normalized_name,
                    candidate.predicate,
                    object_value,
                    candidate.polarity,
                ),
                confidence=candidate.confidence,
                status=status,
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
                stale_state=ClaimStaleState.UNKNOWN,
            )
            session.add(claim)
            session.flush()
            if source is not None:
                evidence = candidate.evidence_span.quote
                session.add(
                    ClaimEvidenceRow(
                        claim_id=claim.id,
                        source_id=source.id,
                        evidence_excerpt=evidence,
                        evidence_hash=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                        support_weight=1.0,
                    )
                )
            claims.append(claim)
        session.flush()
        return claims

    def find_semantic_conflict_memories(
        self, session: Session, candidate: MemoryRow
    ) -> list[MemoryRow]:
        candidate_claims = self.ensure_claims(session, candidate)
        active_claims = list(
            session.scalars(
                select(ClaimRow)
                .join(MemoryRow, MemoryRow.id == ClaimRow.memory_id)
                .where(
                    MemoryRow.scope_type == candidate.scope_type,
                    MemoryRow.scope_key == candidate.scope_key,
                    MemoryRow.status == MemoryStatus.ACTIVE,
                    MemoryRow.id != candidate.id,
                    ClaimRow.status.in_([ClaimStatus.ACCEPTED, ClaimStatus.CONTESTED]),
                )
            )
        )
        entities = {
            row.id: row
            for row in session.scalars(
                select(EntityRow).where(
                    EntityRow.id.in_(
                        {claim.subject_entity_id for claim in [*candidate_claims, *active_claims]}
                    )
                )
            )
        }
        conflicts: dict[str, MemoryRow] = {}
        for candidate_claim in candidate_claims:
            for active_claim in active_claims:
                relation = compare_claim_values(
                    left_subject=entities[candidate_claim.subject_entity_id].normalized_name,
                    left_predicate=candidate_claim.predicate,
                    left_object=self._object_identity(session, candidate_claim),
                    left_polarity=candidate_claim.polarity,
                    left_valid_from=candidate_claim.valid_from,
                    left_valid_to=candidate_claim.valid_to,
                    right_subject=entities[active_claim.subject_entity_id].normalized_name,
                    right_predicate=active_claim.predicate,
                    right_object=self._object_identity(session, active_claim),
                    right_polarity=active_claim.polarity,
                    right_valid_from=active_claim.valid_from,
                    right_valid_to=active_claim.valid_to,
                )
                if relation == "contradicts":
                    memory = session.get(MemoryRow, active_claim.memory_id)
                    if memory is not None:
                        conflicts[memory.id] = memory
        return list(conflicts.values())

    def activate_claims(
        self,
        session: Session,
        memory: MemoryRow,
        *,
        strategy: ConflictStrategy | None,
        conflicts: list[MemoryRow],
        rationale: str | None,
    ) -> None:
        claims = self.ensure_claims(session, memory)
        if strategy is ConflictStrategy.REJECT:
            for claim in claims:
                claim.status = ClaimStatus.REJECTED
            return
        active_claims = list(
            session.scalars(
                select(ClaimRow).where(
                    ClaimRow.memory_id.in_([row.id for row in conflicts] or [""]),
                    ClaimRow.status.in_([ClaimStatus.ACCEPTED, ClaimStatus.CONTESTED]),
                )
            )
        )
        for claim in claims:
            claim.status = ClaimStatus.ACCEPTED
            for active in active_claims:
                if not self._contradicts(session, claim, active):
                    continue
                if strategy is ConflictStrategy.SUPERSEDE:
                    active.status = ClaimStatus.SUPERSEDED
                    active.valid_to = active.valid_to or _now()
                    relation_type = ClaimRelationType.SUPERSEDES
                else:
                    claim.status = ClaimStatus.CONTESTED
                    active.status = ClaimStatus.CONTESTED
                    relation_type = ClaimRelationType.CONTRADICTS
                self._add_relation(
                    session,
                    claim,
                    active,
                    relation_type,
                    method=RelationMethod.MANUAL if strategy else RelationMethod.RULE,
                    explanation=rationale
                    or "Claims overlap on a single-valued semantic dimension.",
                )

    def reject_claims(self, session: Session, memory_id: str) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            claim.status = ClaimStatus.REJECTED

    def reset_claims(
        self,
        session: Session,
        memory: MemoryRow,
        candidates: list[ClaimCandidate] | None = None,
    ) -> list[ClaimRow]:
        claim_ids = list(
            session.scalars(select(ClaimRow.id).where(ClaimRow.memory_id == memory.id))
        )
        if claim_ids:
            session.execute(delete(ClaimRow).where(ClaimRow.id.in_(claim_ids)))
            session.flush()
        return self.ensure_claims(session, memory, candidates)

    def forget_claims(self, session: Session, memory_id: str) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            claim.status = ClaimStatus.HISTORICAL

    def mark_memory_stale(
        self, session: Session, memory_id: str, stale_state: ClaimStaleState
    ) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            claim.stale_state = stale_state
            if stale_state is ClaimStaleState.STALE:
                claim.status = ClaimStatus.STALE

    def current_truth(self, session: Session, request: CurrentTruthRequest) -> dict[str, Any]:
        statement = select(ClaimRow).join(EntityRow, EntityRow.id == ClaimRow.subject_entity_id)
        if request.scope_type is not None:
            statement = statement.where(EntityRow.scope_type == request.scope_type)
        if request.scope_key is not None:
            statement = statement.where(EntityRow.scope_key == request.scope_key)
        rows = list(
            session.scalars(
                statement.where(
                    ClaimRow.status.in_(
                        [
                            ClaimStatus.ACCEPTED,
                            ClaimStatus.CONTESTED,
                            ClaimStatus.SUPERSEDED,
                            ClaimStatus.STALE,
                            ClaimStatus.HISTORICAL,
                        ]
                    )
                )
            )
        )
        valid_moment = request.as_of_valid_time or _now()
        known_moment = request.as_known_at or _now()
        subject_query = normalize_entity_name(request.subject) if request.subject else None
        text_query = normalize_entity_name(request.query) if request.query else None
        visible: list[ClaimRow] = []
        entities: dict[str, EntityRow] = {}
        for claim in rows:
            entity = session.get(EntityRow, claim.subject_entity_id)
            if entity is None:
                continue
            entities[entity.id] = entity
            if subject_query and subject_query not in {
                entity.normalized_name,
                *entity.aliases_json,
            }:
                continue
            if request.predicate and claim.predicate != request.predicate:
                continue
            if text_query:
                haystack = " ".join(
                    (
                        entity.normalized_name,
                        claim.predicate,
                        canonical_object(self._object_identity(session, claim)),
                    )
                )
                if not all(token in haystack for token in text_query.split()):
                    continue
            if not as_of(claim.valid_from, claim.valid_to, valid_moment):
                continue
            if not is_known_at(claim.recorded_at, known_moment):
                continue
            visible.append(claim)

        grouped: dict[tuple[str, str], list[ClaimRow]] = defaultdict(list)
        for claim in visible:
            grouped[(claim.subject_entity_id, claim.predicate)].append(claim)
        truths = []
        all_accepted: list[dict[str, Any]] = []
        all_conflicting: list[dict[str, Any]] = []
        all_evidence: list[dict[str, Any]] = []
        all_history: list[dict[str, Any]] = []
        states: list[TruthState] = []
        for (entity_id, predicate), claims in grouped.items():
            entity = entities[entity_id]
            non_stale = [
                claim
                for claim in claims
                if claim.status is not ClaimStatus.STALE
                and claim.stale_state is not ClaimStaleState.STALE
            ]
            distinct = {
                canonical_object(self._object_identity(session, claim)) for claim in non_stale
            }
            contested = any(claim.status is ClaimStatus.CONTESTED for claim in claims) or (
                is_single_valued(predicate, entity.normalized_name) and len(distinct) > 1
            )
            if contested:
                state = TruthState.CONTESTED
            elif not non_stale and claims:
                state = TruthState.STALE
            elif non_stale:
                state = TruthState.RESOLVED
            else:
                state = TruthState.UNKNOWN
            states.append(state)
            serialized = [self.serialize_claim(session, claim) for claim in claims]
            accepted = [
                item
                for item in serialized
                if item["status"] in {ClaimStatus.ACCEPTED.value, ClaimStatus.CONTESTED.value}
            ]
            conflicting = serialized if state is TruthState.CONTESTED else []
            evidence = self._evidence_for(session, [claim.id for claim in claims])
            history = self._relations_for(session, [claim.id for claim in claims])
            truths.append(
                {
                    "subject": self.serialize_entity(entity),
                    "predicate": predicate,
                    "state": state.value,
                    "accepted_claims": accepted,
                    "conflicting_claims": conflicting,
                    "evidence": evidence,
                    "freshness": sorted({item["stale_state"] for item in serialized}),
                    "resolution_history": history,
                }
            )
            all_accepted.extend(accepted)
            all_conflicting.extend(conflicting)
            all_evidence.extend(evidence)
            all_history.extend(history)
        aggregate = self._aggregate_state(states)
        return {
            "state": aggregate.value,
            "truths": truths,
            "accepted_claims": all_accepted,
            "conflicting_claims": all_conflicting,
            "evidence": all_evidence,
            "freshness": sorted({item["stale_state"] for item in all_accepted}),
            "resolution_history": all_history,
            "as_of_valid_time": valid_moment.isoformat(),
            "as_known_at": known_moment.isoformat(),
        }

    def serialize_claim(self, session: Session, claim: ClaimRow) -> dict[str, Any]:
        subject = session.get(EntityRow, claim.subject_entity_id)
        object_entity = (
            session.get(EntityRow, claim.object_entity_id) if claim.object_entity_id else None
        )
        return {
            "id": claim.id,
            "memory_id": claim.memory_id,
            "subject": self.serialize_entity(subject) if subject else None,
            "predicate": claim.predicate,
            "object_kind": claim.object_kind.value,
            "object_entity": self.serialize_entity(object_entity) if object_entity else None,
            "object_value": claim.object_value,
            "polarity": claim.polarity.value,
            "modality": claim.modality.value,
            "qualifiers": claim.qualifiers_json,
            "canonical_key": claim.canonical_key,
            "confidence": claim.confidence,
            "status": claim.status.value,
            "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
            "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
            "recorded_at": claim.recorded_at.isoformat(),
            "stale_state": claim.stale_state.value,
        }

    @staticmethod
    def serialize_entity(entity: EntityRow) -> dict[str, Any]:
        return {
            "id": entity.id,
            "scope_type": entity.scope_type.value,
            "scope_key": entity.scope_key,
            "entity_type": entity.entity_type.value,
            "canonical_name": entity.canonical_name,
            "normalized_name": entity.normalized_name,
            "aliases": entity.aliases_json,
            "redirect_to_id": entity.redirect_to_id,
        }

    def _contradicts(self, session: Session, left: ClaimRow, right: ClaimRow) -> bool:
        left_subject = session.get(EntityRow, left.subject_entity_id)
        right_subject = session.get(EntityRow, right.subject_entity_id)
        if left_subject is None or right_subject is None:
            return False
        return (
            compare_claim_values(
                left_subject=left_subject.normalized_name,
                left_predicate=left.predicate,
                left_object=self._object_identity(session, left),
                left_polarity=left.polarity,
                left_valid_from=left.valid_from,
                left_valid_to=left.valid_to,
                right_subject=right_subject.normalized_name,
                right_predicate=right.predicate,
                right_object=self._object_identity(session, right),
                right_polarity=right.polarity,
                right_valid_from=right.valid_from,
                right_valid_to=right.valid_to,
            )
            == "contradicts"
        )

    @staticmethod
    def _object_identity(session: Session, claim: ClaimRow) -> Any:
        if claim.object_entity_id:
            entity = session.get(EntityRow, claim.object_entity_id)
            return entity.normalized_name if entity else claim.object_value
        return claim.object_value

    @staticmethod
    def _aggregate_state(states: list[TruthState]) -> TruthState:
        if TruthState.CONTESTED in states:
            return TruthState.CONTESTED
        if TruthState.STALE in states:
            return TruthState.STALE
        if TruthState.RESOLVED in states:
            return TruthState.RESOLVED
        return TruthState.UNKNOWN

    @staticmethod
    def _add_relation(
        session: Session,
        left: ClaimRow,
        right: ClaimRow,
        relation_type: ClaimRelationType,
        *,
        method: RelationMethod,
        explanation: str,
    ) -> None:
        existing = session.scalar(
            select(ClaimRelationRow).where(
                ClaimRelationRow.from_claim_id == left.id,
                ClaimRelationRow.to_claim_id == right.id,
                ClaimRelationRow.relation_type == relation_type,
            )
        )
        if existing is None:
            session.add(
                ClaimRelationRow(
                    from_claim_id=left.id,
                    to_claim_id=right.id,
                    relation_type=relation_type,
                    confidence=1.0,
                    method=method,
                    explanation=explanation,
                )
            )

    @staticmethod
    def _evidence_for(session: Session, claim_ids: list[str]) -> list[dict[str, Any]]:
        if not claim_ids:
            return []
        rows = session.execute(
            select(ClaimEvidenceRow, SourceRow, SourceAnchorRow)
            .join(SourceRow, SourceRow.id == ClaimEvidenceRow.source_id)
            .outerjoin(SourceAnchorRow, SourceAnchorRow.id == ClaimEvidenceRow.source_anchor_id)
            .where(ClaimEvidenceRow.claim_id.in_(claim_ids))
        ).all()
        return [
            {
                "claim_id": evidence.claim_id,
                "source_id": source.id,
                "source_ref": source.source_ref,
                "excerpt": evidence.evidence_excerpt,
                "evidence_hash": evidence.evidence_hash,
                "support_weight": evidence.support_weight,
                "source_anchor_id": anchor.id if anchor else None,
            }
            for evidence, source, anchor in rows
        ]

    @staticmethod
    def _relations_for(session: Session, claim_ids: list[str]) -> list[dict[str, Any]]:
        if not claim_ids:
            return []
        rows = list(
            session.scalars(
                select(ClaimRelationRow).where(
                    or_(
                        ClaimRelationRow.from_claim_id.in_(claim_ids),
                        ClaimRelationRow.to_claim_id.in_(claim_ids),
                    )
                )
            )
        )
        return [
            {
                "id": row.id,
                "from_claim_id": row.from_claim_id,
                "to_claim_id": row.to_claim_id,
                "relation_type": row.relation_type.value,
                "confidence": row.confidence,
                "method": row.method.value,
                "explanation": row.explanation,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
