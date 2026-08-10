from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EpisodeClaim:
    claim_id: str
    memory_id: str
    subject_entity_id: str
    predicate: str
    object_identity: str
    polarity: str
    source_ref: str
    captured_at: datetime
    confidence: float
    payload: dict[str, Any]


def cluster_episodes(rows: list[EpisodeClaim]) -> dict[tuple[str, str], list[EpisodeClaim]]:
    groups: dict[tuple[str, str], list[EpisodeClaim]] = defaultdict(list)
    for row in rows:
        groups[(row.subject_entity_id, row.predicate)].append(row)
    return groups


def independent_source_span_days(rows: list[EpisodeClaim]) -> tuple[int, float]:
    by_source: dict[str, datetime] = {}
    for row in rows:
        captured = by_source.get(row.source_ref)
        if captured is None or row.captured_at < captured:
            by_source[row.source_ref] = row.captured_at
    if not by_source:
        return 0, 0.0
    moments = sorted(by_source.values())
    return len(moments), (moments[-1] - moments[0]).total_seconds() / 86400


def classify_cluster(
    rows: list[EpisodeClaim], *, minimum_sources: int = 3, minimum_span_days: int = 7
) -> str:
    if not rows:
        return "none"
    assertions = Counter((item.object_identity, item.polarity) for item in rows)
    (dominant, dominant_polarity), _ = assertions.most_common(1)[0]
    supporting = [
        item
        for item in rows
        if item.object_identity == dominant and item.polarity == dominant_polarity
    ]
    source_count, span_days = independent_source_span_days(supporting)
    if source_count < minimum_sources or span_days < minimum_span_days:
        return "none"
    counterevidence = any(
        item.object_identity != dominant or item.polarity != dominant_polarity for item in rows
    )
    return "contested" if counterevidence else "candidate"
