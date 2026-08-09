from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from memoryos.claims.canonicalize import canonical_object, canonical_predicate
from memoryos.domain.schemas import ClaimPolarity
from memoryos.temporal.intervals import intervals_overlap

Relationship = Literal["equivalent", "supports", "contradicts", "independent", "uncertain"]


@dataclass(frozen=True)
class RelationshipDecision:
    relationship: Relationship
    confidence: float
    reason: str
    model_eligible: bool = False


@dataclass(frozen=True)
class PredicateDefinition:
    name: str
    cardinality: Literal["single", "multi", "conditional"]
    temporal: bool = True


PREDICATES: dict[str, PredicateDefinition] = {
    "uses": PredicateDefinition("uses", "conditional"),
    "forbidden": PredicateDefinition("forbidden", "multi"),
    "implemented_in": PredicateDefinition("implemented_in", "multi"),
    "prefers": PredicateDefinition("prefers", "single"),
    "failed_because": PredicateDefinition("failed_because", "multi"),
    "states": PredicateDefinition("states", "multi"),
}


def is_single_valued(predicate: str, subject: str) -> bool:
    definition = PREDICATES.get(canonical_predicate(predicate))
    if definition is None:
        return False
    if definition.cardinality == "single":
        return True
    if definition.cardinality == "conditional":
        return any(
            marker in subject
            for marker in ("database", "framework", "package_manager", "current", "primary")
        )
    return False


def compare_claim_values(
    *,
    left_subject: str,
    left_predicate: str,
    left_object: Any,
    left_polarity: ClaimPolarity,
    left_valid_from: Any = None,
    left_valid_to: Any = None,
    right_subject: str,
    right_predicate: str,
    right_object: Any,
    right_polarity: ClaimPolarity,
    right_valid_from: Any = None,
    right_valid_to: Any = None,
) -> Relationship:
    decision = classify_claim_values(
        left_subject=left_subject,
        left_predicate=left_predicate,
        left_object=left_object,
        left_polarity=left_polarity,
        left_valid_from=left_valid_from,
        left_valid_to=left_valid_to,
        right_subject=right_subject,
        right_predicate=right_predicate,
        right_object=right_object,
        right_polarity=right_polarity,
        right_valid_from=right_valid_from,
        right_valid_to=right_valid_to,
    )
    return "independent" if decision.relationship == "uncertain" else decision.relationship


def classify_claim_values(
    *,
    left_subject: str,
    left_predicate: str,
    left_object: Any,
    left_polarity: ClaimPolarity,
    left_valid_from: Any = None,
    left_valid_to: Any = None,
    right_subject: str,
    right_predicate: str,
    right_object: Any,
    right_polarity: ClaimPolarity,
    right_valid_from: Any = None,
    right_valid_to: Any = None,
) -> RelationshipDecision:
    if left_subject != right_subject or canonical_predicate(left_predicate) != canonical_predicate(
        right_predicate
    ):
        return RelationshipDecision("independent", 1.0, "Subjects or canonical predicates differ")
    if not intervals_overlap(left_valid_from, left_valid_to, right_valid_from, right_valid_to):
        return RelationshipDecision("independent", 1.0, "Valid-time intervals do not overlap")
    left_value = canonical_object(left_object)
    right_value = canonical_object(right_object)
    if left_value == right_value and left_polarity == right_polarity:
        return RelationshipDecision("equivalent", 1.0, "Canonical objects and polarity match")
    if (
        left_polarity == right_polarity
        and isinstance(left_object, str)
        and isinstance(right_object, str)
        and (left_value in right_value or right_value in left_value)
    ):
        return RelationshipDecision("supports", 0.88, "One normalized statement contains the other")
    if left_value == right_value and left_polarity != right_polarity:
        return RelationshipDecision(
            "contradicts", 1.0, "The same object is asserted with opposite polarity"
        )
    if is_single_valued(left_predicate, left_subject):
        return RelationshipDecision(
            "contradicts", 0.97, "Different values overlap on a single-valued dimension"
        )
    return RelationshipDecision(
        "uncertain",
        0.5,
        "Same semantic slot and overlapping time, but deterministic rules are inconclusive",
        model_eligible=True,
    )
