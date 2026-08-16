from src.middleware.request_context import available_trace_values


def test_traceparent_is_collected_when_present() -> None:
    values = available_trace_values({"traceparent": "00-fixture"})
    assert values == {"traceparent": "00-fixture"}
