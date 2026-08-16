from __future__ import annotations

KNOWN_TRACE_HEADERS = ("traceparent", "X-Request-ID")


def available_trace_values(headers: dict[str, str]) -> dict[str, str]:
    """Return trace-related values present on an incoming request."""

    return {name: headers[name] for name in KNOWN_TRACE_HEADERS if name in headers}
