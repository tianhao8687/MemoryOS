from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from memoryos.context.token_meter import TokenCounter, canonical_json
from memoryos.domain.schemas import DetailLevel, FreshnessState, TruthState
from memoryos.retrieval.context import _section

ATOM_RENDER_POLICY_VERSION = "context-atom-v1"
CURRENT_STRUCTURED_STATUSES = {"accepted", "contested", "stale"}
HISTORICAL_STRUCTURED_STATUS = "historical"
MINIMUM_ATOM_UTILITY = 1e-6


class CompressionPolicy(StrEnum):
    PINNED = "pinned"
    SOURCE_GROUNDED = "source_grounded"
    COMPRESSIBLE = "compressible"
    EPHEMERAL = "ephemeral"


class ContextAtom(BaseModel):
    """A deterministic compiled view; it is never a source of truth itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str
    memory_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    canonical_key: str
    bundle_key: str
    atom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail_level: DetailLevel
    compression_policy: CompressionPolicy
    rendered_text: str
    fact_text: str
    truth_state: TruthState
    freshness: FreshnessState
    valid_from: datetime | None
    valid_to: datetime | None
    recorded_at: datetime | None
    evidence_count: int = Field(ge=0)
    source_refs: tuple[str, ...]
    evidence_pointer_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    utility: float
    estimated_tokens: int = Field(ge=0)
    category: str
    section: str
    modality: str
    polarity: str
    status: str

    @property
    def required(self) -> bool:
        return self.compression_policy is CompressionPolicy.PINNED

    def snapshot_item(self, ordinal: int) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "memory_id": self.memory_id,
            "memory_ids": list(self.memory_ids),
            "atom_sha256": self.atom_sha256,
            "bundle_key": self.bundle_key,
            "truth_state": self.truth_state.value,
            "freshness": self.freshness.value,
            "detail_level": self.detail_level.value,
            "compression_policy": self.compression_policy.value,
            "rendered_text": self.rendered_text,
            "section": self.section,
            "evidence_pointer_version": self.evidence_pointer_version,
        }


class AtomBuilder:
    def __init__(self, counter: TokenCounter) -> None:
        self.counter = counter

    def build(
        self,
        candidate: dict[str, Any],
        metadata: dict[str, Any],
        *,
        requested_detail: DetailLevel = DetailLevel.FACT,
        include_historical: bool = False,
    ) -> list[ContextAtom]:
        candidate_claim_ids = candidate.get("claim_ids")
        allowed_claim_ids = (
            None if candidate_claim_ids is None else {str(value) for value in candidate_claim_ids}
        )
        allowed_statuses = set(CURRENT_STRUCTURED_STATUSES)
        if include_historical:
            allowed_statuses.add(HISTORICAL_STRUCTURED_STATUS)
        claims = sorted(
            [
                claim
                for claim in metadata.get("claims", [])
                if str(claim.get("status", "")) in allowed_statuses
                and (allowed_claim_ids is None or str(claim.get("id")) in allowed_claim_ids)
            ],
            key=lambda claim: str(claim.get("id", "")),
        )
        if claims and requested_detail is not DetailLevel.INDEX:
            return [self._structured(candidate, metadata, claim) for claim in claims]
        return [self._unstructured(candidate, metadata, requested_detail)]

    def _structured(
        self,
        candidate: dict[str, Any],
        metadata: dict[str, Any],
        claim: dict[str, Any],
    ) -> ContextAtom:
        memory = candidate["memory"]
        category = str(memory["category"]).strip().lower()
        modality = str(claim["modality"])
        polarity = str(claim["polarity"])
        is_constraint = category == "constraint" or modality == "constraint"
        fact_text = str(memory["content"]).strip() if is_constraint else _render_claim(claim)
        policy = CompressionPolicy.PINNED if is_constraint else CompressionPolicy.COMPRESSIBLE
        detail = DetailLevel.FACT
        valid_from = _as_datetime(claim.get("valid_from"))
        valid_to = _as_datetime(claim.get("valid_to"))
        identity = {
            "subject_entity_id": claim["subject_entity_id"],
            "predicate": claim["predicate"],
            "object_kind": claim["object_kind"],
            "object_entity_id": claim.get("object_entity_id"),
            "object_value": claim.get("object_value"),
            "polarity": polarity,
            "qualifiers": claim.get("qualifiers", {}),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "modality": modality,
            "claim_status": str(claim["status"]),
            "truth_state": candidate["truth_state"],
            "freshness": candidate["trace"]["freshness"],
            "constraint_text": _normalize_text(str(memory["content"])) if is_constraint else None,
        }
        canonical_key = _digest(identity)
        bundle_key = _digest(
            {
                "subject_entity_id": claim["subject_entity_id"],
                "predicate": claim["predicate"],
            }
        )
        return self._make_atom(
            candidate,
            metadata,
            claim_ids=(str(claim["id"]),),
            canonical_key=canonical_key,
            bundle_key=bundle_key,
            detail_level=detail,
            compression_policy=policy,
            fact_text=fact_text,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_at=_as_datetime(claim.get("recorded_at")),
            category=category,
            modality=modality,
            polarity=polarity,
            status=str(claim["status"]),
        )

    def _unstructured(
        self,
        candidate: dict[str, Any],
        metadata: dict[str, Any],
        requested_detail: DetailLevel,
    ) -> ContextAtom:
        memory = candidate["memory"]
        category = str(memory["category"]).strip().lower()
        source_grounded = category in {"constraint", "state", "task_state"}
        if source_grounded:
            policy = (
                CompressionPolicy.PINNED
                if category == "constraint"
                else CompressionPolicy.SOURCE_GROUNDED
            )
            detail = DetailLevel.FACT
            fact_text = str(memory["content"]).strip()
        else:
            policy = CompressionPolicy.EPHEMERAL
            detail = DetailLevel.INDEX
            fact_text = f"Relevant record: {str(memory['title']).strip()}"
        if requested_detail is DetailLevel.INDEX and policy is not CompressionPolicy.PINNED:
            detail = DetailLevel.INDEX
            fact_text = f"Relevant record: {str(memory['title']).strip()}"
        memory_id = str(memory["id"])
        canonical_key = _digest(
            {
                "memory_id": memory_id,
                "content": _normalize_text(str(memory["content"])),
                "updated_at": memory.get("updated_at"),
                "truth_state": candidate["truth_state"],
                "freshness": candidate["trace"]["freshness"],
                "modality": "constraint" if category == "constraint" else "record",
            }
        )
        return self._make_atom(
            candidate,
            metadata,
            claim_ids=(),
            canonical_key=canonical_key,
            bundle_key=f"memory:{memory_id}",
            detail_level=detail,
            compression_policy=policy,
            fact_text=fact_text,
            valid_from=_as_datetime(memory.get("valid_from")),
            valid_to=_as_datetime(memory.get("valid_to")),
            recorded_at=_as_datetime(memory.get("updated_at") or memory.get("created_at")),
            category=category,
            modality="constraint" if category == "constraint" else "record",
            polarity="unspecified",
            status=str(memory["status"]),
        )

    def _make_atom(
        self,
        candidate: dict[str, Any],
        metadata: dict[str, Any],
        *,
        claim_ids: tuple[str, ...],
        canonical_key: str,
        bundle_key: str,
        detail_level: DetailLevel,
        compression_policy: CompressionPolicy,
        fact_text: str,
        valid_from: datetime | None,
        valid_to: datetime | None,
        recorded_at: datetime | None,
        category: str,
        modality: str,
        polarity: str,
        status: str,
    ) -> ContextAtom:
        memory = candidate["memory"]
        memory_id = str(memory["id"])
        available_pointers = list(metadata.get("evidence_pointers", []))
        claim_pointers = [
            pointer
            for pointer in available_pointers
            if pointer.get("claim_id") is not None and str(pointer.get("claim_id")) in claim_ids
        ]
        generic_pointers = [
            pointer for pointer in available_pointers if pointer.get("claim_id") is None
        ]
        pointers = sorted(
            claim_pointers if claim_ids and claim_pointers else generic_pointers,
            key=canonical_json,
        )
        pointer_source_refs = {
            str(pointer["source_ref"])
            for pointer in pointers
            if pointer.get("source_ref") is not None
        }
        source_refs = tuple(
            sorted(pointer_source_refs or {str(value) for value in metadata.get("source_refs", [])})
        )
        pointer_version = _digest(pointers)
        truth_state = TruthState(str(candidate["truth_state"]))
        freshness = FreshnessState(str(candidate["trace"]["freshness"]))
        atom_hash = _digest(
            {
                "fact": _normalize_text(fact_text),
                "claim_ids": sorted(claim_ids),
                "polarity": polarity,
                "status": status,
                "qualifiers": [
                    claim.get("qualifiers", {})
                    for claim in metadata.get("claims", [])
                    if str(claim.get("id")) in claim_ids
                ],
                "truth_state": truth_state.value,
                "freshness": freshness.value,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "evidence_pointer_version": pointer_version,
                "render_policy_version": ATOM_RENDER_POLICY_VERSION,
                "detail_level": detail_level.value,
            }
        )
        pointer_sources = {
            str(pointer.get("source_id") or pointer.get("source_ref") or canonical_json(pointer))
            for pointer in pointers
        }
        evidence_count = (
            len(pointer_sources)
            if pointer_sources
            else max(
                int(candidate["trace"].get("evidence_count", 0)),
                len(source_refs),
            )
        )
        rendered = _render_atom_line(
            memory_id=memory_id,
            atom_sha256=atom_hash,
            fact_text=fact_text,
            truth_state=truth_state,
            freshness=freshness,
            evidence_count=evidence_count,
            policy=compression_policy,
            detail_level=detail_level,
            status=status,
        )
        return ContextAtom(
            memory_id=memory_id,
            memory_ids=(memory_id,),
            claim_ids=claim_ids,
            canonical_key=canonical_key,
            bundle_key=bundle_key,
            atom_sha256=atom_hash,
            detail_level=detail_level,
            compression_policy=compression_policy,
            rendered_text=rendered,
            fact_text=fact_text,
            truth_state=truth_state,
            freshness=freshness,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_at=recorded_at,
            evidence_count=evidence_count,
            source_refs=source_refs,
            evidence_pointer_version=pointer_version,
            utility=max(float(candidate.get("score", 0.0)), MINIMUM_ATOM_UTILITY),
            estimated_tokens=self.counter.count_text(rendered),
            category=category,
            section=_section(memory),
            modality=modality,
            polarity=polarity,
            status=status,
        )


def exact_deduplicate(
    atoms: list[ContextAtom], counter: TokenCounter
) -> tuple[list[ContextAtom], dict[str, str]]:
    """Merge only byte-canonical fact identities and preserve all evidence pointers."""

    grouped: dict[str, list[ContextAtom]] = {}
    for atom in atoms:
        grouped.setdefault(atom.canonical_key, []).append(atom)
    result: list[ContextAtom] = []
    duplicate_of: dict[str, str] = {}
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (-item.utility, item.memory_id, item.atom_sha256))
        primary = ordered[0]
        if len(ordered) == 1:
            result.append(primary)
            continue
        memory_ids = tuple(sorted({value for item in ordered for value in item.memory_ids}))
        claim_ids = tuple(sorted({value for item in ordered for value in item.claim_ids}))
        source_refs = tuple(sorted({value for item in ordered for value in item.source_refs}))
        pointer_version = _digest(sorted({item.evidence_pointer_version for item in ordered}))
        evidence_count = (
            len(source_refs)
            if source_refs
            else sum(
                max(item.evidence_count for item in pointer_group)
                for pointer_group in _group_by_pointer_version(ordered)
            )
        )
        atom_hash = _digest(
            {
                "canonical_key": primary.canonical_key,
                "fact": _normalize_text(primary.fact_text),
                "truth_state": primary.truth_state.value,
                "freshness": primary.freshness.value,
                "valid_from": primary.valid_from,
                "valid_to": primary.valid_to,
                "evidence_pointer_version": pointer_version,
                "render_policy_version": ATOM_RENDER_POLICY_VERSION,
            }
        )
        rendered = _render_atom_line(
            memory_id=primary.memory_id,
            atom_sha256=atom_hash,
            fact_text=primary.fact_text,
            truth_state=primary.truth_state,
            freshness=primary.freshness,
            evidence_count=evidence_count,
            policy=primary.compression_policy,
            detail_level=primary.detail_level,
            status=primary.status,
        )
        merged = primary.model_copy(
            update={
                "memory_ids": memory_ids,
                "claim_ids": claim_ids,
                "atom_sha256": atom_hash,
                "rendered_text": rendered,
                "evidence_count": evidence_count,
                "source_refs": source_refs,
                "evidence_pointer_version": pointer_version,
                "estimated_tokens": counter.count_text(rendered),
            }
        )
        for duplicate in ordered[1:]:
            duplicate_of[duplicate.memory_id] = merged.memory_id
        result.append(merged)
    result.sort(key=lambda item: (-item.utility, item.memory_id, item.atom_sha256))
    return result, duplicate_of


def _render_claim(claim: dict[str, Any]) -> str:
    subject = str(claim.get("subject") or claim["subject_entity_id"])
    predicate = str(claim["predicate"])
    raw_object = claim.get("object_name")
    if raw_object is None:
        raw_object = claim.get("object_value")
    object_text = canonical_json(raw_object) if not isinstance(raw_object, str) else raw_object
    statement = f"{subject} {predicate} {object_text}".strip()
    if str(claim["polarity"]) == "negative":
        statement = f"NOT ({statement})"
    qualifiers = claim.get("qualifiers", {})
    if qualifiers:
        statement += f"; qualifiers={canonical_json(qualifiers)}"
    return statement


def _group_by_pointer_version(atoms: list[ContextAtom]) -> list[list[ContextAtom]]:
    grouped: dict[str, list[ContextAtom]] = {}
    for atom in atoms:
        grouped.setdefault(atom.evidence_pointer_version, []).append(atom)
    return list(grouped.values())


def _render_atom_line(
    *,
    memory_id: str,
    atom_sha256: str,
    fact_text: str,
    truth_state: TruthState,
    freshness: FreshnessState,
    evidence_count: int,
    policy: CompressionPolicy,
    detail_level: DetailLevel,
    status: str,
) -> str:
    verification = (
        "; verify_before_use=true"
        if freshness
        in {
            FreshnessState.SUSPECT,
            FreshnessState.STALE,
        }
        else ""
    )
    fact_label = "record" if detail_level is DetailLevel.INDEX else "fact"
    status_label = (
        f"; status={status}"
        if status in {HISTORICAL_STRUCTURED_STATUS, "superseded", "expired"}
        else ""
    )
    return (
        f"- [{memory_id} @ {atom_sha256}] {fact_text}\n"
        f"  {fact_label}; state={truth_state.value}/{freshness.value}; "
        f"policy={policy.value}; evidence={evidence_count}; details=memory_explain"
        f"{status_label}{verification}"
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


__all__ = [
    "ATOM_RENDER_POLICY_VERSION",
    "CURRENT_STRUCTURED_STATUSES",
    "HISTORICAL_STRUCTURED_STATUS",
    "MINIMUM_ATOM_UTILITY",
    "AtomBuilder",
    "CompressionPolicy",
    "ContextAtom",
    "exact_deduplicate",
]
