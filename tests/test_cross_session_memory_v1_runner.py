from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from memoryos.evaluation.provider_usage import CachePhase, ProviderUsageRecord, UsageSource
from scripts import run_cross_session_memory_v1 as runner


def test_remote_memory_backend_authenticates_context_requests(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_http_json(
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        token: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        captured.update(
            {
                "url": url,
                "method": method,
                "body": body,
                "token": token,
                "timeout": timeout,
            }
        )
        return {"items": [], "compiled_context": ""}

    monkeypatch.setattr(runner, "_http_json", fake_http_json)
    backend = runner.RemoteMemoryBackend(
        base_url="http://127.0.0.1:8765",
        token="local-bearer-token",
        task="recall the decision",
        repository="fixture://repository",
        budget_tokens=1200,
    )
    arguments = {
        "task": "recall the decision",
        "scope": {
            "type": "repository",
            "key": "fixture://repository",
        },
        "budget_tokens": 1200,
    }

    result = backend.execute("memory_context", arguments)

    assert result["ok"] is True
    assert captured == {
        "url": "http://127.0.0.1:8765/api/context",
        "method": "POST",
        "body": arguments,
        "token": "local-bearer-token",
        "timeout": 30,
    }


def test_list_memories_without_status_explicitly_requests_superseded_history(
    monkeypatch: Any, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_http_json(
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        token: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        captured.update({"url": url, "method": method, "token": token})
        return {"items": []}

    monkeypatch.setattr(runner, "_http_json", fake_http_json)
    process = runner.MemoryOSProcess(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        project_root=tmp_path,
    )
    process.base_url = "http://127.0.0.1:8765"
    process.token = "local-bearer-token"

    assert process.list_memories("fixture://repository", status=None) == []
    assert "include_history=true" in captured["url"]
    assert "status=" not in captured["url"]
    assert captured["method"] == "GET"
    assert captured["token"] == "local-bearer-token"


def _usage(step: int, input_tokens: int) -> ProviderUsageRecord:
    return ProviderUsageRecord(
        run_id="write-token-run",
        task_id="write-token-task",
        condition="msc_context_only",
        cache_phase=CachePhase.COLD,
        session_id="session-804f9116-1a60-4ff6-90c3-216bdd8cefc6",
        step_index=step,
        provider="deepseek",
        model="deepseek-v4-flash",
        input_tokens=input_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=input_tokens,
        output_tokens=10,
        reasoning_tokens=5,
        cost_usd=Decimal("0.001"),
        latency_seconds=1,
        usage_source=UsageSource.PROVIDER_EXACT,
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        request_bytes=100,
        cache_namespace_sha256="c" * 64,
    )


def test_session_a_write_token_accounting_keeps_estimates_separate_from_provider_usage(
    tmp_path: Path,
) -> None:
    attempts_path = tmp_path / "provider-attempts.jsonl"
    accounting = [
        {
            "tokenizer_id": "unicode-heuristic-v1",
            "tokenizer_kind": "estimated",
            "counter_version": "1.0.0",
            "write_tool_schema_tokens": 100,
            "memory_write_result_tokens": 0,
            "memory_write_visible_tokens": 100,
        },
        {
            "tokenizer_id": "unicode-heuristic-v1",
            "tokenizer_kind": "estimated",
            "counter_version": "1.0.0",
            "write_tool_schema_tokens": 100,
            "memory_write_result_tokens": 50,
            "memory_write_visible_tokens": 150,
        },
    ]
    attempts_path.write_text(
        "".join(json.dumps({"memory_write_token_accounting": item}) + "\n" for item in accounting),
        encoding="utf-8",
    )
    turn = runner.TurnRun(
        turn=1,
        prompt="ordinary source turn",
        session_id="session-804f9116-1a60-4ff6-90c3-216bdd8cefc6",
        status="completed",
        failure_reason=None,
        output="done",
        provider_attempts=2,
        usage=(_usage(0, 120), _usage(1, 180)),
        tool_events=(),
        write_token_attempts=runner._read_write_token_attempts(attempts_path),
    )

    summary = runner._source_write_token_summary([turn])

    assert summary["write_tool_schema_tokens"] == 100
    assert summary["cumulative_write_tool_schema_tokens"] == 200
    assert summary["memory_write_result_tokens"] == 50
    assert summary["memory_write_visible_tokens"] == 250
    assert summary["provider_input_tokens"] == 300
    assert summary["write_component_token_source"] == "estimated"
    assert summary["provider_input_token_source"] == "provider_exact"
    assert summary["complete_accounting"] is True
