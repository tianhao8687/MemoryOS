from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from memoryos.db.models import EntityMergeEventRow, EntityRow
from memoryos.domain.schemas import EntityType, ScopeType
from memoryos.entities.aliases import normalize_entity_name
from memoryos.errors import NotFoundError


class EntityResolver:
    """Deterministic-first scoped entity resolution with auditable redirects."""

    def resolve(
        self,
        session: Session,
        *,
        scope_type: ScopeType,
        scope_key: str,
        entity_type: EntityType,
        name: str,
        aliases: list[str] | None = None,
        stable_external_key: str | None = None,
    ) -> EntityRow:
        normalized = normalize_entity_name(stable_external_key or name)
        row = session.scalar(
            select(EntityRow).where(
                EntityRow.scope_type == scope_type,
                EntityRow.scope_key == scope_key,
                EntityRow.entity_type == entity_type,
                EntityRow.normalized_name == normalized,
            )
        )
        if row is not None:
            combined = {normalize_entity_name(value) for value in row.aliases_json}
            combined.update(normalize_entity_name(value) for value in aliases or [])
            combined.add(normalize_entity_name(name))
            row.aliases_json = sorted(value for value in combined if value != normalized)
            return self.follow_redirect(session, row)
        row = EntityRow(
            scope_type=scope_type,
            scope_key=scope_key,
            entity_type=entity_type,
            canonical_name=name.strip(),
            normalized_name=normalized,
            aliases_json=sorted(
                {
                    normalize_entity_name(value)
                    for value in aliases or []
                    if normalize_entity_name(value) != normalized
                }
            ),
            stable_external_key=stable_external_key,
        )
        session.add(row)
        session.flush()
        return row

    def merge_candidates(
        self,
        session: Session,
        *,
        scope_type: ScopeType,
        scope_key: str,
        entity_type: EntityType,
        name: str,
        minimum_similarity: float = 0.65,
    ) -> list[dict[str, Any]]:
        normalized = normalize_entity_name(name)
        rows = list(
            session.scalars(
                select(EntityRow).where(
                    EntityRow.scope_type == scope_type,
                    EntityRow.scope_key == scope_key,
                    EntityRow.entity_type == entity_type,
                    EntityRow.redirect_to_id.is_(None),
                )
            )
        )
        matches = []
        for row in rows:
            candidates = [row.normalized_name, *row.aliases_json]
            similarity = max(
                SequenceMatcher(None, normalized, candidate).ratio() for candidate in candidates
            )
            if similarity >= minimum_similarity and similarity < 1.0:
                matches.append(
                    {
                        "entity_id": row.id,
                        "canonical_name": row.canonical_name,
                        "similarity": round(similarity, 6),
                        "decision": "merge_candidate",
                    }
                )
        return sorted(matches, key=lambda item: float(item["similarity"]), reverse=True)

    def merge(
        self,
        session: Session,
        source_id: str,
        target_id: str,
        *,
        actor: str,
        rationale: str | None = None,
    ) -> EntityRow:
        source = session.get(EntityRow, source_id)
        target = session.get(EntityRow, target_id)
        if source is None or target is None:
            raise NotFoundError("entity merge source or target was not found")
        if (
            source.scope_type != target.scope_type
            or source.scope_key != target.scope_key
            or source.entity_type != target.entity_type
        ):
            raise ValueError("entities may only merge inside the same scope and type")
        target.aliases_json = sorted(
            {
                *target.aliases_json,
                *source.aliases_json,
                source.normalized_name,
                normalize_entity_name(source.canonical_name),
            }
        )
        source.redirect_to_id = target.id
        session.add(
            EntityMergeEventRow(
                from_entity_id=source.id,
                to_entity_id=target.id,
                actor=actor,
                rationale=rationale,
            )
        )
        session.flush()
        return target

    @staticmethod
    def follow_redirect(session: Session, entity: EntityRow) -> EntityRow:
        seen = {entity.id}
        current = entity
        while current.redirect_to_id:
            if current.redirect_to_id in seen:
                raise ValueError("entity redirect cycle detected")
            seen.add(current.redirect_to_id)
            target = session.get(EntityRow, current.redirect_to_id)
            if target is None:
                raise NotFoundError("entity redirect target was not found")
            current = target
        return current
