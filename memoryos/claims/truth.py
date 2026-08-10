from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from memoryos.claims.canonicalize import (
    canonical_claim_key,
    canonical_object,
    extract_claim_candidates,
    validate_claim_candidates,
)
from memoryos.claims.predicates import (
    RelationshipDecision,
    classify_claim_values,
    compare_claim_values,
    is_single_valued,
)
from memoryos.claims.versioning import ClaimVersionStore
from memoryos.db.models import (
    AuditEventRow,
    ClaimEvidenceRow,
    ClaimIdentityRow,
    ClaimRelationRow,
    ClaimRow,
    ClaimVersionRow,
    EntityRow,
    MemoryHealthRow,
    MemoryRow,
    PossibleConflictRow,
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
    MemoryTemperature,
    PossibleConflictStatus,
    RelationMethod,
    TruthState,
)
from memoryos.entities import EntityResolver, normalize_entity_name
from memoryos.errors import ProviderError
from memoryos.providers.base import RelationshipJudge


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TruthMaintenanceService:
    """Claim lifecycle, semantic relationships, and bitemporal current truth."""

    def __init__(self, relationship_judge: RelationshipJudge | None = None) -> None:
        self.entities = EntityResolver()
        self.versions = ClaimVersionStore()
        self.relationship_judge = relationship_judge

    def ensure_claims(
        self,
        session: Session,
        memory: MemoryRow,
        candidates: list[ClaimCandidate] | None = None,
    ) -> list[ClaimRow]:
        existing = list(
            session.scalars(
                select(ClaimRow).where(
                    ClaimRow.memory_id == memory.id,
                    ClaimRow.status.not_in([ClaimStatus.HISTORICAL, ClaimStatus.REJECTED]),
                )
            )
        )
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
            self.versions.record_initial(
                session,
                claim,
                memory,
                actor="system:claim-extractor",
                reason="Evidence-bound claim extracted",
            )
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
        active_pairs = list(
            session.execute(
                select(ClaimRow, MemoryRow)
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
        now = _now()
        active_claims = [
            claim
            for claim, memory in active_pairs
            if (claim.valid_to is None or now < _utc(claim.valid_to))
            and (
                memory.ttl_seconds is None
                or _utc(memory.created_at) + timedelta(seconds=memory.ttl_seconds) > now
            )
        ]
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
                decision = classify_claim_values(
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
                if self._is_conflict_decision(
                    session,
                    candidate_claim,
                    active_claim,
                    decision,
                ):
                    memory = session.get(MemoryRow, active_claim.memory_id)
                    if memory is not None:
                        conflicts[memory.id] = memory
        return list(conflicts.values())

    def _is_conflict_decision(
        self,
        session: Session,
        left: ClaimRow,
        right: ClaimRow,
        decision: RelationshipDecision,
    ) -> bool:
        if decision.relationship == "contradicts":
            return True
        if not decision.model_eligible:
            return False
        pair = tuple(sorted((left.id, right.id)))
        existing = session.scalar(
            select(PossibleConflictRow).where(
                PossibleConflictRow.left_claim_id == pair[0],
                PossibleConflictRow.right_claim_id == pair[1],
            )
        )
        if existing is not None:
            return existing.status is PossibleConflictStatus.CONFIRMED

        evidence = self._evidence_for(session, [left.id, right.id])
        bounded_evidence = [
            {
                "claim_id": item["claim_id"],
                "source_ref": item["source_ref"],
                "excerpt": str(item["excerpt"])[:1000],
                "evidence_hash": item["evidence_hash"],
            }
            for item in evidence[:8]
        ]
        evidence_hash = hashlib.sha256(
            json.dumps(bounded_evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        status = PossibleConflictStatus.POSSIBLE
        result: dict[str, Any] = {
            "relationship": "uncertain",
            "confidence": decision.confidence,
            "explanation": decision.reason,
            "abstain": True,
        }
        fingerprint = None
        prompt_version = None
        if self.relationship_judge is not None:
            metadata = self.relationship_judge.metadata
            fingerprint_payload = {
                "provider": metadata.provider,
                "model": metadata.model,
                "capabilities": metadata.capabilities,
            }
            digest = hashlib.sha256(
                json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
            ).hexdigest()
            fingerprint = f"{metadata.provider}:{metadata.model}:{digest[:16]}"
            prompt_version = "relationship-judge-v2.1.0"
            try:
                result = self.relationship_judge.judge(
                    self._judge_claim(session, left),
                    self._judge_claim(session, right),
                    bounded_evidence,
                )
                relationship = str(result.get("relationship", "uncertain"))
                confidence = float(result.get("confidence", 0.0))
                abstain = bool(result.get("abstain", False))
                if (
                    not abstain
                    and confidence >= 0.7
                    and relationship
                    in {
                        "contradicts",
                        "supersedes_candidate",
                    }
                ):
                    status = PossibleConflictStatus.CONFIRMED
                elif (
                    not abstain
                    and confidence >= 0.7
                    and relationship
                    in {
                        "equivalent",
                        "supports",
                        "independent",
                    }
                ):
                    status = PossibleConflictStatus.DISMISSED
                else:
                    status = PossibleConflictStatus.ABSTAINED
            except ProviderError as exc:
                status = PossibleConflictStatus.ABSTAINED
                result = {
                    "relationship": "uncertain",
                    "confidence": 0.0,
                    "explanation": str(exc),
                    "abstain": True,
                }
        row = PossibleConflictRow(
            left_claim_id=pair[0],
            right_claim_id=pair[1],
            status=status,
            deterministic_relationship=decision.relationship,
            deterministic_confidence=decision.confidence,
            reason=decision.reason,
            model_result_json=result,
            provider_fingerprint=fingerprint,
            prompt_version=prompt_version,
            evidence_hash=evidence_hash,
        )
        session.add(row)
        session.flush()
        if status is PossibleConflictStatus.CONFIRMED:
            self._add_relation(
                session,
                left,
                right,
                ClaimRelationType.CONTRADICTS,
                method=RelationMethod.MODEL_JUDGE,
                explanation=str(result.get("explanation", "Model-confirmed conflict")),
                confidence=float(result.get("confidence", 0.0)),
            )
            return True
        return False

    def _judge_claim(self, session: Session, claim: ClaimRow) -> dict[str, Any]:
        subject = session.get(EntityRow, claim.subject_entity_id)
        return {
            "claim_id": claim.id,
            "subject": subject.normalized_name if subject else claim.subject_entity_id,
            "predicate": claim.predicate,
            "object": self._object_identity(session, claim),
            "polarity": claim.polarity.value,
            "modality": claim.modality.value,
            "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
            "valid_to": claim.valid_to.isoformat() if claim.valid_to else None,
        }

    def activate_claims(
        self,
        session: Session,
        memory: MemoryRow,
        *,
        strategy: ConflictStrategy | None,
        conflicts: list[MemoryRow],
        rationale: str | None,
        actor: str = "manual",
    ) -> None:
        claims = self.ensure_claims(session, memory)
        if strategy is ConflictStrategy.REJECT:
            for claim in claims:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.REJECTED,
                    actor=actor,
                    reason=rationale or "Candidate rejected during conflict resolution",
                )
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
            if claim.status is not ClaimStatus.ACCEPTED:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.ACCEPTED,
                    actor=actor,
                    reason=rationale or "Candidate accepted",
                )
            for active in active_claims:
                if not self._claims_conflict(session, claim, active):
                    continue
                if strategy is ConflictStrategy.SUPERSEDE:
                    if active.status is not ClaimStatus.SUPERSEDED:
                        self.versions.transition(
                            session,
                            active,
                            status=ClaimStatus.SUPERSEDED,
                            valid_to=active.valid_to or _now(),
                            actor=actor,
                            reason=rationale or f"Superseded by claim {claim.id}",
                        )
                    relation_type = ClaimRelationType.SUPERSEDES
                else:
                    if claim.status is not ClaimStatus.CONTESTED:
                        self.versions.transition(
                            session,
                            claim,
                            status=ClaimStatus.CONTESTED,
                            actor=actor,
                            reason=rationale or f"Conflicts with claim {active.id}",
                        )
                    if active.status is not ClaimStatus.CONTESTED:
                        self.versions.transition(
                            session,
                            active,
                            status=ClaimStatus.CONTESTED,
                            actor=actor,
                            reason=rationale or f"Conflicts with claim {claim.id}",
                        )
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

    def reject_claims(
        self,
        session: Session,
        memory_id: str,
        *,
        actor: str = "manual",
        reason: str = "Candidate rejected",
    ) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            if claim.status is not ClaimStatus.REJECTED:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.REJECTED,
                    actor=actor,
                    reason=reason,
                )

    def reset_claims(
        self,
        session: Session,
        memory: MemoryRow,
        candidates: list[ClaimCandidate] | None = None,
    ) -> list[ClaimRow]:
        claims = list(session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory.id)))
        for claim in claims:
            if claim.status not in {ClaimStatus.HISTORICAL, ClaimStatus.REJECTED}:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.HISTORICAL,
                    actor="manual:edit",
                    reason="Candidate content was edited; prior extraction retained as history",
                )
        return self.ensure_claims(session, memory, candidates)

    def forget_claims(self, session: Session, memory_id: str, *, actor: str = "manual") -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            if claim.status is not ClaimStatus.HISTORICAL:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.HISTORICAL,
                    actor=actor,
                    reason="Memory forgotten; claim retained for historical reconstruction",
                )

    def expire_claims(
        self,
        session: Session,
        memory_id: str,
        *,
        reason: str,
        at: datetime,
    ) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            if claim.status not in {ClaimStatus.HISTORICAL, ClaimStatus.REJECTED}:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.HISTORICAL,
                    valid_to=claim.valid_to or at,
                    actor="system:expiry",
                    reason=reason,
                    at=at,
                )

    def mark_memory_stale(
        self,
        session: Session,
        memory_id: str,
        stale_state: ClaimStaleState,
        *,
        actor: str = "system:freshness",
    ) -> None:
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.memory_id == memory_id)):
            status = ClaimStatus.STALE if stale_state is ClaimStaleState.STALE else None
            if claim.stale_state is not stale_state or (status and claim.status is not status):
                self.versions.transition(
                    session,
                    claim,
                    stale_state=stale_state,
                    status=status,
                    actor=actor,
                    reason=f"Freshness evaluation changed to {stale_state.value}",
                )

    def apply_manual_conflict_resolution(
        self,
        session: Session,
        *,
        left_claim_id: str,
        right_claim_id: str,
        confirmed: bool,
        actor: str,
        rationale: str | None,
    ) -> None:
        """Project a reviewed possible-conflict decision into live claim truth."""
        if not confirmed:
            return
        left = session.get(ClaimRow, left_claim_id)
        right = session.get(ClaimRow, right_claim_id)
        if left is None or right is None:
            raise ValueError("possible conflict references a missing claim")
        explanation = rationale or "Possible conflict manually confirmed"
        self._add_relation(
            session,
            left,
            right,
            ClaimRelationType.CONTRADICTS,
            method=RelationMethod.MANUAL,
            explanation=explanation,
        )
        memories = {
            memory.id: memory
            for memory in (
                session.get(MemoryRow, left.memory_id),
                session.get(MemoryRow, right.memory_id),
            )
            if memory is not None
        }
        if any(memory.status is not MemoryStatus.ACTIVE for memory in memories.values()):
            return
        for claim, other in ((left, right), (right, left)):
            if claim.status is ClaimStatus.ACCEPTED:
                self.versions.transition(
                    session,
                    claim,
                    status=ClaimStatus.CONTESTED,
                    actor=actor,
                    reason=f"Manual conflict confirmation against claim {other.id}: {explanation}",
                )

    def current_truth(self, session: Session, request: CurrentTruthRequest) -> dict[str, Any]:
        valid_moment = request.as_of_valid_time or _now()
        known_moment = request.as_known_at or _now()
        rows = self.versions.visible_versions(
            session,
            valid_time=valid_moment,
            known_time=known_moment,
        )
        subject_query = normalize_entity_name(request.subject) if request.subject else None
        text_query = normalize_entity_name(request.query) if request.query else None
        visible: list[ClaimVersionRow] = []
        identities: dict[str, ClaimIdentityRow] = {}
        entities: dict[str, EntityRow] = {}
        visible_statuses = {
            ClaimStatus.ACCEPTED,
            ClaimStatus.CONTESTED,
            ClaimStatus.SUPERSEDED,
            ClaimStatus.STALE,
            ClaimStatus.HISTORICAL,
        }
        for version in rows:
            if version.status not in visible_statuses:
                continue
            if self._memory_archived_at(session, version.memory_id, known_moment):
                continue
            identity = session.get(ClaimIdentityRow, version.identity_id)
            if identity is None:
                continue
            entity = session.get(EntityRow, identity.subject_entity_id)
            if entity is None:
                continue
            if request.scope_type is not None and identity.scope_type is not request.scope_type:
                continue
            if request.scope_key is not None and identity.scope_key != request.scope_key:
                continue
            identities[identity.id] = identity
            entities[entity.id] = entity
            if subject_query and subject_query not in {
                entity.normalized_name,
                *entity.aliases_json,
            }:
                continue
            if request.predicate and identity.canonical_predicate != request.predicate:
                continue
            if text_query:
                haystack = " ".join(
                    (
                        entity.normalized_name,
                        identity.canonical_predicate,
                        canonical_object(self._version_object_identity(session, version)),
                    )
                )
                if not all(token in haystack for token in text_query.split()):
                    continue
            visible.append(version)

        grouped: dict[str, list[ClaimVersionRow]] = defaultdict(list)
        for version in visible:
            grouped[version.identity_id].append(version)
        truths = []
        all_accepted: list[dict[str, Any]] = []
        all_conflicting: list[dict[str, Any]] = []
        all_evidence: list[dict[str, Any]] = []
        all_history: list[dict[str, Any]] = []
        states: list[TruthState] = []
        for identity_id, versions in grouped.items():
            identity = identities[identity_id]
            entity = entities[identity.subject_entity_id]
            non_stale = [
                version
                for version in versions
                if version.status
                in {
                    ClaimStatus.ACCEPTED,
                    ClaimStatus.CONTESTED,
                }
                and version.stale_state is not ClaimStaleState.STALE
            ]
            distinct = {
                canonical_object(self._version_object_identity(session, version))
                for version in non_stale
            }
            contested = any(version.status is ClaimStatus.CONTESTED for version in versions) or (
                is_single_valued(identity.canonical_predicate, entity.normalized_name)
                and len(distinct) > 1
            )
            if contested:
                state = TruthState.CONTESTED
            elif not non_stale and versions:
                state = TruthState.STALE
            elif non_stale:
                state = TruthState.RESOLVED
            else:
                state = TruthState.UNKNOWN
            states.append(state)
            serialized = [
                self.serialize_version(session, version, identity) for version in versions
            ]
            accepted = [
                item
                for item in serialized
                if item["status"] in {ClaimStatus.ACCEPTED.value, ClaimStatus.CONTESTED.value}
            ]
            conflicting = serialized if state is TruthState.CONTESTED else []
            claim_ids = [version.claim_id for version in versions]
            evidence = self._evidence_for(session, claim_ids)
            history = [
                *self._version_history(session, claim_ids, known_moment),
                *self._relations_for(session, claim_ids),
            ]
            truths.append(
                {
                    "identity_id": identity.id,
                    "subject": self.serialize_entity(entity),
                    "predicate": identity.canonical_predicate,
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

    @staticmethod
    def _memory_archived_at(session: Session, memory_id: str, known_moment: datetime) -> bool:
        events = list(
            session.execute(
                select(AuditEventRow.action, AuditEventRow.timestamp)
                .where(
                    AuditEventRow.entity_type == "memory",
                    AuditEventRow.entity_id == memory_id,
                    AuditEventRow.action.in_(["health_archive", "health_restore"]),
                )
                .order_by(AuditEventRow.timestamp)
            )
        )
        if events:
            known_utc = _utc(known_moment)
            visible_actions = [
                action for action, timestamp in events if _utc(timestamp) <= known_utc
            ]
            return bool(visible_actions and visible_actions[-1] == "health_archive")
        health = session.get(MemoryHealthRow, memory_id)
        if health is None or health.temperature is not MemoryTemperature.ARCHIVED:
            return False
        return health.archived_at is None or _utc(health.archived_at) <= _utc(known_moment)

    def serialize_version(
        self,
        session: Session,
        version: ClaimVersionRow,
        identity: ClaimIdentityRow | None = None,
    ) -> dict[str, Any]:
        resolved_identity = identity or session.get(ClaimIdentityRow, version.identity_id)
        subject = (
            session.get(EntityRow, resolved_identity.subject_entity_id)
            if resolved_identity is not None
            else None
        )
        object_entity = (
            session.get(EntityRow, version.object_entity_id) if version.object_entity_id else None
        )
        return {
            "id": version.claim_id,
            "version_id": version.id,
            "version_number": version.version_number,
            "identity_id": version.identity_id,
            "memory_id": version.memory_id,
            "subject": self.serialize_entity(subject) if subject else None,
            "predicate": resolved_identity.canonical_predicate if resolved_identity else None,
            "object_kind": version.object_kind.value,
            "object_entity": self.serialize_entity(object_entity) if object_entity else None,
            "object_value": version.object_value,
            "polarity": version.polarity.value,
            "modality": version.modality.value,
            "qualifiers": version.qualifiers_json,
            "confidence": version.confidence,
            "status": version.status.value,
            "valid_from": version.valid_from.isoformat() if version.valid_from else None,
            "valid_to": version.valid_to.isoformat() if version.valid_to else None,
            "transaction_from": version.transaction_from.isoformat(),
            "transaction_to": (
                version.transaction_to.isoformat() if version.transaction_to else None
            ),
            "stale_state": version.stale_state.value,
            "reason": version.reason,
            "actor": version.actor,
            "source_event_id": version.source_event_id,
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

    def _claims_conflict(self, session: Session, left: ClaimRow, right: ClaimRow) -> bool:
        if self._contradicts(session, left, right):
            return True
        pair = tuple(sorted((left.id, right.id)))
        confirmed = session.scalar(
            select(PossibleConflictRow.id).where(
                PossibleConflictRow.left_claim_id == pair[0],
                PossibleConflictRow.right_claim_id == pair[1],
                PossibleConflictRow.status == PossibleConflictStatus.CONFIRMED,
            )
        )
        return confirmed is not None

    @staticmethod
    def _object_identity(session: Session, claim: ClaimRow) -> Any:
        if claim.object_entity_id:
            entity = session.get(EntityRow, claim.object_entity_id)
            return entity.normalized_name if entity else claim.object_value
        return claim.object_value

    @staticmethod
    def _version_object_identity(session: Session, version: ClaimVersionRow) -> Any:
        if version.object_entity_id:
            entity = session.get(EntityRow, version.object_entity_id)
            return entity.normalized_name if entity else version.object_value
        return version.object_value

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
        confidence: float = 1.0,
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
                    confidence=confidence,
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

    @staticmethod
    def _version_history(
        session: Session,
        claim_ids: list[str],
        known_moment: datetime,
    ) -> list[dict[str, Any]]:
        if not claim_ids:
            return []
        rows = list(
            session.scalars(
                select(ClaimVersionRow)
                .where(
                    ClaimVersionRow.claim_id.in_(claim_ids),
                    ClaimVersionRow.transaction_from <= known_moment,
                )
                .order_by(
                    ClaimVersionRow.transaction_from.asc(),
                    ClaimVersionRow.version_number.asc(),
                )
            )
        )
        return [
            {
                "type": "claim_version",
                "version_id": row.id,
                "claim_id": row.claim_id,
                "version_number": row.version_number,
                "status": row.status.value,
                "stale_state": row.stale_state.value,
                "transaction_from": row.transaction_from.isoformat(),
                "transaction_to": row.transaction_to.isoformat() if row.transaction_to else None,
                "reason": row.reason,
                "actor": row.actor,
            }
            for row in rows
        ]
