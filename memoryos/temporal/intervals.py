from __future__ import annotations

from datetime import UTC, datetime


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def as_of(valid_from: datetime | None, valid_to: datetime | None, moment: datetime) -> bool:
    point = _utc(moment)
    start = _utc(valid_from)
    end = _utc(valid_to)
    assert point is not None
    return (start is None or start <= point) and (end is None or point < end)


def is_known_at(recorded_at: datetime, moment: datetime) -> bool:
    recorded = _utc(recorded_at)
    point = _utc(moment)
    assert recorded is not None and point is not None
    return recorded <= point


def intervals_overlap(
    left_start: datetime | None,
    left_end: datetime | None,
    right_start: datetime | None,
    right_end: datetime | None,
) -> bool:
    left_start_utc = _utc(left_start) or datetime.min.replace(tzinfo=UTC)
    right_start_utc = _utc(right_start) or datetime.min.replace(tzinfo=UTC)
    left_end_utc = _utc(left_end) or datetime.max.replace(tzinfo=UTC)
    right_end_utc = _utc(right_end) or datetime.max.replace(tzinfo=UTC)
    return max(left_start_utc, right_start_utc) < min(left_end_utc, right_end_utc)
