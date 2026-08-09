from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select

from memoryos.claims.canonicalize import canonical_object
from memoryos.claims.truth import TruthMaintenanceService
from memoryos.consolidation.cluster import (
    EpisodeClaim,
    cluster_episodes,
    independent_source_span_days,
)
from memoryos.db.models import (
    ClaimRow,
    ConsolidationCandidateRow,
    MemoryRow,
    MemorySourceRow,
    SourceRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import ConsolidateRequest, MemoryStatus, MemoryType


class ConsolidationService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.truth = TruthMaintenanceService()

    def propose(self, request: ConsolidateRequest) -> dict[str, Any]:
        with self.database.session() as session:
            rows = session.execute(
                select(ClaimRow, MemoryRow, SourceRow)
                .join(MemoryRow, MemoryRow.id == ClaimRow.memory_id)
                .join(MemorySourceRow, MemorySourceRow.memory_id == MemoryRow.id)
                .join(SourceRow, SourceRow.id == MemorySourceRow.source_id)
                .where(
                    MemoryRow.scope_type == request.scope_type,
                    MemoryRow.scope_key == request.scope_key,
                    MemoryRow.memory_type == MemoryType.EPISODIC,
                    MemoryRow.status == MemoryStatus.ACTIVE,
                )
            ).all()
            episodes = [
                EpisodeClaim(
                    claim_id=claim.id,
                    memory_id=memory.id,
                    subject_entity_id=claim.subject_entity_id,
                    predicate=claim.predicate,
                    object_identity=canonical_object(self.truth._object_identity(session, claim)),
                    polarity=claim.polarity.value,
                    source_ref=source.source_ref,
                    captured_at=source.captured_at,
                    confidence=claim.confidence,
                    payload=self.truth.serialize_claim(session, claim),
                )
                for claim, memory, source in rows
            ]
            proposals = []
            for (subject_entity_id, predicate), group in cluster_episodes(episodes).items():
                source_count, span_days = independent_source_span_days(group)
                if source_count < request.minimum_sources or span_days < request.minimum_span_days:
                    continue
                objects = Counter(item.object_identity for item in group)
                dominant, dominant_count = objects.most_common(1)[0]
                supporting = [item for item in group if item.object_identity == dominant]
                counterevidence = [
                    item
                    for item in group
                    if item.object_identity != dominant or item.polarity != supporting[0].polarity
                ]
                if len({item.source_ref for item in supporting}) < request.minimum_sources:
                    continue
                status = "contested" if counterevidence else "candidate"
                source_memory_ids = list(dict.fromkeys(item.memory_id for item in supporting))
                proposal_payload = {
                    "subject_entity_id": subject_entity_id,
                    "predicate": predicate,
                    "object": dominant,
                    "confidence": round(
                        sum(item.confidence for item in supporting) / len(supporting), 6
                    ),
                    "independent_sources": source_count,
                    "span_days": round(span_days, 3),
                    "supporting_episodes": dominant_count,
                    "activation": "human_confirmation_required",
                }
                serialized_counter = [item.payload for item in counterevidence]
                persisted_id = None
                if not request.dry_run:
                    row = ConsolidationCandidateRow(
                        scope_type=request.scope_type,
                        scope_key=request.scope_key,
                        subject_entity_id=subject_entity_id,
                        predicate=predicate,
                        proposal_json=proposal_payload,
                        status=status,
                        source_memory_ids=source_memory_ids,
                        counterevidence_json=serialized_counter,
                    )
                    session.add(row)
                    session.flush()
                    persisted_id = row.id
                proposals.append(
                    {
                        "id": persisted_id,
                        "status": status,
                        "proposal": proposal_payload,
                        "source_memory_ids": source_memory_ids,
                        "relations": [
                            {
                                "relation_type": "consolidated_from",
                                "memory_id": memory_id,
                            }
                            for memory_id in source_memory_ids
                        ],
                        "counterevidence": serialized_counter,
                    }
                )
            return {
                "scope_type": request.scope_type.value,
                "scope_key": request.scope_key,
                "dry_run": request.dry_run,
                "proposals": proposals,
                "count": len(proposals),
            }
