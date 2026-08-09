from __future__ import annotations

import json
import re
from typing import Any

from memoryos.domain.schemas import (
    ClaimCandidate,
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    EntityType,
    EvidenceSpan,
)
from memoryos.entities.aliases import normalize_entity_name

PREDICATE_ALIASES = {
    "use": "uses",
    "uses": "uses",
    "adopts": "uses",
    "database": "uses",
    "decision": "uses",
    "forbid": "forbidden",
    "forbids": "forbidden",
    "constraint": "forbidden",
    "prefer": "prefers",
    "preference": "prefers",
    "failure": "failed_because",
    "implemented": "implemented_in",
    "implementation": "implemented_in",
}

KNOWN_TECH_TYPES = {
    "postgresql": EntityType.DATABASE,
    "sqlite": EntityType.DATABASE,
    "mysql": EntityType.DATABASE,
    "redis": EntityType.DEPENDENCY,
    "fastapi": EntityType.DEPENDENCY,
    "django": EntityType.DEPENDENCY,
    "react": EntityType.DEPENDENCY,
    "pnpm": EntityType.TOOL,
    "npm": EntityType.TOOL,
    "yarn": EntityType.TOOL,
}

TECH_PATTERN = re.compile(
    r"(?i)\b(postgres(?:ql)?|sqlite3?|mysql|redis|fast\s*api|django|react|pnpm|npm|yarn)\b"
)
PATH_PATTERN = re.compile(r"(?<![\w.])(?:[\w.-]+/)+[\w.#+-]+")


def canonical_predicate(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return PREDICATE_ALIASES.get(normalized, normalized or "states")


def canonical_object(value: Any) -> str:
    if isinstance(value, str):
        return normalize_entity_name(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_claim_key(
    subject: str, predicate: str, object_value: Any, polarity: ClaimPolarity
) -> str:
    return "|".join(
        (
            normalize_entity_name(subject),
            canonical_predicate(predicate),
            canonical_object(object_value),
            polarity.value,
        )
    )


def _dimension(text: str, fallback: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("database", "数据库", "postgres", "sqlite", "mysql")):
        return "project.production_database"
    if any(token in lower for token in ("framework", "框架", "fastapi", "django")):
        return "project.backend_framework"
    if any(token in lower for token in ("package manager", "包管理", "pnpm", "npm", "yarn")):
        return "project.package_manager"
    if "redis" in lower:
        return "project.dependencies.redis"
    return fallback


def _modality(category: str) -> ClaimModality:
    mapping = {
        "decision": ClaimModality.DECISION,
        "constraint": ClaimModality.CONSTRAINT,
        "preference": ClaimModality.PREFERENCE,
        "failure": ClaimModality.FAILURE,
        "observation": ClaimModality.OBSERVATION,
    }
    return mapping.get(category.lower(), ClaimModality.FACT)


def _evidence(source: str, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(start=start, end=end, quote=source[start:end])


def _candidate_for_technology(
    source: str,
    segment: str,
    segment_start: int,
    technology_match: re.Match[str],
    *,
    predicate: str,
    modality: ClaimModality,
    fallback_subject: str,
) -> ClaimCandidate:
    raw = technology_match.group(0)
    technology = normalize_entity_name(raw.replace(" ", ""))
    entity_type = KNOWN_TECH_TYPES.get(technology, EntityType.DEPENDENCY)
    subject = _dimension(segment, fallback_subject)
    if predicate == "forbidden":
        subject = f"project.dependencies.{technology}"
    start = segment_start
    end = segment_start + len(segment)
    return ClaimCandidate(
        subject_hint=subject,
        subject_type=EntityType.PROJECT,
        predicate=predicate,
        object_kind=ClaimObjectKind.ENTITY,
        object_entity_hint=technology,
        object_entity_type=entity_type,
        polarity=ClaimPolarity.POSITIVE,
        modality=modality,
        confidence=0.88,
        evidence_span=_evidence(source, start, end),
    )


def extract_claim_candidates(
    text: str,
    *,
    title: str = "",
    category: str = "fact",
    key: str | None = None,
    subject: str | None = None,
) -> list[ClaimCandidate]:
    """Extract conservative, evidence-bound claims without inventing absent facts."""

    source = text.strip()
    if not source:
        return []
    fallback_subject = subject or key or title or "project"
    candidates: list[ClaimCandidate] = []
    segment_pattern = re.compile(
        r"[^\n.!?。！？;；]+[\n.!?。！？;；]?",  # noqa: RUF001
        re.UNICODE,
    )
    for segment_match in segment_pattern.finditer(source):
        segment = segment_match.group(0).strip()
        if not segment:
            continue
        lower = segment.lower()
        technology_matches = list(TECH_PATTERN.finditer(segment))
        is_forbidden = bool(
            re.search(r"(?i)\b(do not|don't|never|must not|forbid)\b", segment)
        ) or any(token in segment for token in ("不要", "不得", "禁止"))
        is_preference = (
            bool(re.search(r"(?i)\b(prefer|preferred|preference)\b", segment)) or "偏好" in segment
        )
        is_use = bool(
            re.search(r"(?i)\b(use|uses|using|adopt|choose|chose|decided?)\b", segment)
        ) or any(token in segment for token in ("使用", "采用", "决定", "改为"))
        is_failure = bool(re.search(r"(?i)\b(failed|failure|caused|because)\b", segment)) or any(
            token in segment for token in ("失败", "因为", "导致")
        )
        if is_forbidden and technology_matches:
            for match in technology_matches:
                candidates.append(
                    _candidate_for_technology(
                        source,
                        segment,
                        segment_match.start(),
                        match,
                        predicate="forbidden",
                        modality=ClaimModality.CONSTRAINT,
                        fallback_subject=fallback_subject,
                    )
                )
            continue
        if is_preference and technology_matches:
            candidates.append(
                _candidate_for_technology(
                    source,
                    segment,
                    segment_match.start(),
                    technology_matches[-1],
                    predicate="prefers",
                    modality=ClaimModality.PREFERENCE,
                    fallback_subject=fallback_subject,
                )
            )
            continue
        if is_use and technology_matches:
            candidates.append(
                _candidate_for_technology(
                    source,
                    segment,
                    segment_match.start(),
                    technology_matches[0],
                    predicate="uses",
                    modality=ClaimModality.DECISION,
                    fallback_subject=fallback_subject,
                )
            )
        path_match = PATH_PATTERN.search(segment)
        if path_match and any(token in lower for token in ("implement", "located", "moved")):
            candidates.append(
                ClaimCandidate(
                    subject_hint=fallback_subject,
                    subject_type=EntityType.SYMBOL,
                    predicate="implemented_in",
                    object_kind=ClaimObjectKind.LITERAL,
                    object_value=path_match.group(0),
                    modality=ClaimModality.FACT,
                    confidence=0.82,
                    evidence_span=_evidence(
                        source, segment_match.start(), segment_match.start() + len(segment)
                    ),
                )
            )
        if is_failure:
            candidates.append(
                ClaimCandidate(
                    subject_hint=_dimension(segment, fallback_subject),
                    subject_type=EntityType.PROJECT,
                    predicate="failed_because",
                    object_kind=ClaimObjectKind.LITERAL,
                    object_value=segment,
                    modality=ClaimModality.FAILURE,
                    confidence=0.78,
                    evidence_span=_evidence(
                        source, segment_match.start(), segment_match.start() + len(segment)
                    ),
                )
            )
    if not candidates:
        predicate_by_category = {
            "decision": "uses",
            "constraint": "forbidden",
            "preference": "prefers",
            "failure": "failed_because",
            "implementation": "implemented_in",
        }
        candidates.append(
            ClaimCandidate(
                subject_hint=_dimension(f"{title} {source}", fallback_subject),
                subject_type=EntityType.PROJECT,
                predicate=predicate_by_category.get(category.lower(), "states"),
                object_kind=ClaimObjectKind.LITERAL,
                object_value=source,
                modality=_modality(category),
                confidence=0.65,
                evidence_span=_evidence(source, 0, len(source)),
            )
        )
    unique: dict[tuple[str, str, str], ClaimCandidate] = {}
    for candidate in candidates:
        object_identity = candidate.object_entity_hint or canonical_object(candidate.object_value)
        unique[(candidate.subject_hint, candidate.predicate, object_identity)] = candidate
    return list(unique.values())


def validate_claim_candidates(
    candidates: list[ClaimCandidate], source: str
) -> tuple[list[ClaimCandidate], list[dict[str, Any]]]:
    valid: list[ClaimCandidate] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        span = candidate.evidence_span
        reason = None
        if span.end > len(source):
            reason = "evidence_out_of_bounds"
        elif source[span.start : span.end] != span.quote:
            reason = "evidence_quote_mismatch"
        elif not candidate.predicate.strip():
            reason = "empty_predicate"
        if reason:
            rejected.append({"candidate": candidate.model_dump(mode="json"), "reason": reason})
        else:
            valid.append(candidate)
    return valid, rejected
