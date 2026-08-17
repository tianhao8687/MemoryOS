from __future__ import annotations


def attach_trace_metadata(event: dict[str, object], values: dict[str, str]) -> None:
    event["trace"] = dict(values)
