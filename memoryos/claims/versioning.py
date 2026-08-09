from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memoryos.claims.canonicalize import canonical_predicate
from memoryos.db.models import ClaimIdentityRow, ClaimRow, ClaimVersionRow, EntityRow, MemoryRow
from memoryos.domain.schemas import ClaimStaleState, ClaimStatus


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stable_claim_identity(
    scope_type: str,
    scope_key: str,
    subject_entity_id: str,
    predicate: str,
) -> str:
    payload = "|".join((scope_type, scope_key, subject_entity_id, canonical_predicate(predicate)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ClaimVersionStore:
    """Append transaction-time snapshots while keeping ``claims`` as a current projection."""

    def identity_for(
        self,
        session: Session,
        claim: ClaimRow,
        memory: MemoryRow | None = None,
    ) -> ClaimIdentityRow:
        subject = session.get(EntityRow, claim.subject_entity_id)
        if subject is None:
            raise ValueError(f"claim {claim.id} references a missing subject entity")
        scope_type = subject.scope_type
        scope_key = subject.scope_key
        stable = stable_claim_identity(
            scope_type.value,
            scope_key,
            subject.id,
            claim.predicate,
        )
        identity = session.scalar(
            select(ClaimIdentityRow).where(
                ClaimIdentityRow.scope_type == scope_type,
                ClaimIdentityRow.scope_key == scope_key,
                ClaimIdentityRow.stable_identity == stable,
            )
        )
        if identity is not None:
            return identity
        identity = ClaimIdentityRow(
            scope_type=scope_type,
            scope_key=scope_key,
            subject_entity_id=subject.id,
            canonical_subject=subject.normalized_name,
            canonical_predicate=canonical_predicate(claim.predicate),
            stable_identity=stable,
            created_at=claim.recorded_at,
        )
        session.add(identity)
        session.flush()
        return identity

    def record_initial(
        self,
        session: Session,
        claim: ClaimRow,
        memory: MemoryRow,
        *,
        actor: str,
        reason: str,
        at: datetime | None = None,
        source_event_id: str | None = None,
    ) -> ClaimVersionRow:
        existing = session.scalar(
            select(ClaimVersionRow).where(ClaimVersionRow.claim_id == claim.id).limit(1)
        )
        if existing is not None:
            return existing
        identity = self.identity_for(session, claim, memory)
        version = self._snapshot(
            claim,
            identity_id=identity.id,
            version_number=1,
            actor=actor,
            reason=reason,
            at=at or claim.recorded_at,
            source_event_id=source_event_id,
        )
        session.add(version)
        session.flush()
        return version

    def transition(
        self,
        session: Session,
        claim: ClaimRow,
        *,
        actor: str,
        reason: str,
        status: ClaimStatus | None = None,
        stale_state: ClaimStaleState | None = None,
        valid_to: datetime | object | None = ...,
        at: datetime | None = None,
        source_event_id: str | None = None,
    ) -> ClaimVersionRow:
        memory = session.get(MemoryRow, claim.memory_id)
        if memory is None:
            raise ValueError(f"claim {claim.id} references a missing memory")
        current = session.scalar(
            select(ClaimVersionRow)
            .where(
                ClaimVersionRow.claim_id == claim.id,
                ClaimVersionRow.transaction_to.is_(None),
            )
            .order_by(ClaimVersionRow.version_number.desc())
        )
        if current is None:
            current = self.record_initial(
                session,
                claim,
                memory,
                actor="system:compatibility",
                reason="Lazy V2.1 history backfill",
            )
        moment = at or _now()
        if _utc(moment) <= _utc(current.transaction_from):
            moment = _utc(current.transaction_from) + timedelta(microseconds=1)
        current.transaction_to = moment
        if status is not None:
            claim.status = status
        if stale_state is not None:
            claim.stale_state = stale_state
        if valid_to is not ...:
            claim.valid_to = valid_to  # type: ignore[assignment]
        next_number = (
            int(
                session.scalar(
                    select(func.max(ClaimVersionRow.version_number)).where(
                        ClaimVersionRow.claim_id == claim.id
                    )
                )
                or 0
            )
            + 1
        )
        version = self._snapshot(
            claim,
            identity_id=current.identity_id,
            version_number=next_number,
            actor=actor,
            reason=reason,
            at=moment,
            source_event_id=source_event_id,
        )
        session.add(version)
        session.flush()
        return version

    def visible_versions(
        self,
        session: Session,
        *,
        valid_time: datetime,
        known_time: datetime,
    ) -> list[ClaimVersionRow]:
        candidates = list(
            session.scalars(
                select(ClaimVersionRow)
                .where(ClaimVersionRow.transaction_from <= known_time)
                .order_by(
                    ClaimVersionRow.claim_id,
                    ClaimVersionRow.transaction_from.desc(),
                    ClaimVersionRow.version_number.desc(),
                )
            )
        )
        current_at_time: dict[str, ClaimVersionRow] = {}
        for version in candidates:
            if version.claim_id in current_at_time:
                continue
            if version.transaction_to is not None and _utc(version.transaction_to) <= _utc(
                known_time
            ):
                continue
            current_at_time[version.claim_id] = version
        return [
            version
            for version in current_at_time.values()
            if (version.valid_from is None or _utc(version.valid_from) <= _utc(valid_time))
            and (version.valid_to is None or _utc(valid_time) < _utc(version.valid_to))
        ]

    @staticmethod
    def _snapshot(
        claim: ClaimRow,
        *,
        identity_id: str,
        version_number: int,
        actor: str,
        reason: str,
        at: datetime,
        source_event_id: str | None,
    ) -> ClaimVersionRow:
        return ClaimVersionRow(
            claim_id=claim.id,
            identity_id=identity_id,
            memory_id=claim.memory_id,
            version_number=version_number,
            object_kind=claim.object_kind,
            object_entity_id=claim.object_entity_id,
            object_value=claim.object_value,
            polarity=claim.polarity,
            modality=claim.modality,
            qualifiers_json=dict(claim.qualifiers_json),
            valid_from=claim.valid_from,
            valid_to=claim.valid_to,
            transaction_from=at,
            status=claim.status,
            stale_state=claim.stale_state,
            confidence=claim.confidence,
            reason=reason,
            actor=actor,
            source_event_id=source_event_id,
            created_at=at,
        )
