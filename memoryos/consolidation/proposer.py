from __future__ import annotations

import hashlib
import json
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
from memoryos.domain.schemas import (
    ClaimStaleState,
    ClaimStatus,
    ConsolidateRequest,
    MemoryStatus,
    MemoryType,
)
from memoryos.errors import ProviderError
from memoryos.providers.base import ConsolidationJudge


class ConsolidationService:
    def __init__(
        self,
        database: Database,
        judge: ConsolidationJudge | None = None,
    ) -> None:
        self.database = database
        self.truth = TruthMaintenanceService()
        self.judge = judge

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
                    ClaimRow.status.in_([ClaimStatus.ACCEPTED, ClaimStatus.CONTESTED]),
                    ClaimRow.stale_state != ClaimStaleState.STALE,
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
                assertions = Counter((item.object_identity, item.polarity) for item in group)
                (dominant, dominant_polarity), _ = assertions.most_common(1)[0]
                supporting = [
                    item
                    for item in group
                    if item.object_identity == dominant and item.polarity == dominant_polarity
                ]
                counterevidence = [
                    item
                    for item in group
                    if item.object_identity != dominant or item.polarity != dominant_polarity
                ]
                source_count, span_days = independent_source_span_days(supporting)
                if source_count < request.minimum_sources or span_days < request.minimum_span_days:
                    continue
                status = "contested" if counterevidence else "candidate"
                source_memory_ids = list(dict.fromkeys(item.memory_id for item in supporting))
                proposal_payload = {
                    "subject_entity_id": subject_entity_id,
                    "predicate": predicate,
                    "object": dominant,
                    "polarity": dominant_polarity,
                    "confidence": round(
                        sum(item.confidence for item in supporting) / len(supporting), 6
                    ),
                    "independent_sources": source_count,
                    "span_days": round(span_days, 3),
                    "supporting_episodes": len(supporting),
                    "activation": "human_confirmation_required",
                    "abstraction_mode": "offline-extractive-fallback",
                    "supporting_memory_ids": source_memory_ids,
                    "counterevidence_memory_ids": list(
                        dict.fromkeys(item.memory_id for item in counterevidence)
                    ),
                }
                judged = self._judge_cluster(
                    group,
                    request.minimum_sources,
                    request.minimum_span_days,
                )
                if judged is not None:
                    status = judged["status"]
                    source_memory_ids = judged["supporting_memory_ids"]
                    counter_ids = judged["counterevidence_memory_ids"]
                    counterevidence = [item for item in group if item.memory_id in counter_ids]
                    proposal_payload = {
                        **proposal_payload,
                        "abstraction": judged["proposal"],
                        "confidence": judged["confidence"],
                        "abstraction_mode": "grounded-model",
                        "supporting_memory_ids": source_memory_ids,
                        "counterevidence_memory_ids": counter_ids,
                        "provider_fingerprint": judged["provider_fingerprint"],
                        "prompt_version": "consolidation-judge-v2.1.0",
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

    def _judge_cluster(
        self,
        group: list[EpisodeClaim],
        minimum_sources: int,
        minimum_span_days: int,
    ) -> dict[str, Any] | None:
        if self.judge is None:
            return None
        episodes = [
            {
                "memory_id": item.memory_id,
                "claim_id": item.claim_id,
                "claim": item.payload,
                "source_ref": item.source_ref,
                "captured_at": item.captured_at.isoformat(),
            }
            for item in group
        ]
        try:
            result = self.judge.judge(episodes)
        except ProviderError:
            return None
        if result.get("status") == "abstain" or not str(result.get("proposal") or "").strip():
            return None
        allowed = {item.memory_id for item in group}
        raw_supporting = [str(item) for item in result.get("supporting_memory_ids", [])]
        raw_counters = [str(item) for item in result.get("counterevidence_memory_ids", [])]
        if any(item not in allowed for item in [*raw_supporting, *raw_counters]) or set(
            raw_supporting
        ) & set(raw_counters):
            return None
        supporting = list(dict.fromkeys(raw_supporting))
        supporting_rows = [item for item in group if item.memory_id in supporting]
        dominant_assertion, _ = Counter(
            (item.object_identity, item.polarity) for item in group
        ).most_common(1)[0]
        supporting_assertions = {(item.object_identity, item.polarity) for item in supporting_rows}
        if supporting_assertions != {dominant_assertion}:
            return None
        automatic_counters = [
            item.memory_id
            for item in group
            if (item.object_identity, item.polarity) != dominant_assertion
        ]
        counters = list(dict.fromkeys([*raw_counters, *automatic_counters]))
        source_count, span_days = independent_source_span_days(supporting_rows)
        if source_count < minimum_sources or span_days < minimum_span_days:
            return None
        metadata = self.judge.metadata
        fingerprint = hashlib.sha256(
            json.dumps(
                {"provider": metadata.provider, "model": metadata.model},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        status = "contested" if counters or result.get("status") == "contested" else "candidate"
        return {
            "status": status,
            "proposal": str(result["proposal"]),
            "supporting_memory_ids": supporting,
            "counterevidence_memory_ids": counters,
            "confidence": float(result.get("confidence", 0.0)),
            "provider_fingerprint": f"{metadata.provider}:{metadata.model}:{fingerprint[:16]}",
        }
