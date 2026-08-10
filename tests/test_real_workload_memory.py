from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from memoryos.config import settings_for
from memoryos.db import Database
from memoryos.db.models import MemoryRow
from memoryos.evaluation.real_workload_memory import MemoryRuntime, MemoryRuntimeBuilder
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    MemorySeedSpec,
    WorkloadTaskSpec,
)

IMAGE = "python:3.12-slim@sha256:" + "a" * 64


def _task() -> WorkloadTaskSpec:
    return WorkloadTaskSpec.model_validate(
        {
            "id": "parser-fix",
            "repository_id": "project",
            "sequence_id": "parser",
            "sequence_index": 1,
            "base_commit": "1" * 40,
            "solution_commit": "2" * 40,
            "cutoff": "2025-02-01T00:00:00Z",
            "prompt": "Fix parser result handling.",
            "memory_seed_ids": ["helpful", "stale", "cross-project"],
            "hidden_test": {"image": IMAGE, "command": ["python", "-m", "pytest"]},
        }
    )


def _seeds() -> list[MemorySeedSpec]:
    common = {
        "memory_type": "project",
        "category": "decision",
        "captured_at": "2025-01-10T00:00:00Z",
        "source_commit": "0" * 40,
        "source_ref": "docs/decisions.md",
    }
    return [
        MemorySeedSpec.model_validate(
            {
                **common,
                "id": "helpful",
                "repository_id": "project",
                "title": "Parser result decision",
                "content": "The parser must return an explicit Result object.",
                "valid_to": "2025-03-01T00:00:00Z",
                "expectation": "helpful",
            }
        ),
        MemorySeedSpec.model_validate(
            {
                **common,
                "id": "stale",
                "repository_id": "project",
                "title": "Old parser decision",
                "content": "The parser used to return a raw string.",
                "valid_to": "2025-01-20T00:00:00Z",
                "expectation": "stale",
            }
        ),
        MemorySeedSpec.model_validate(
            {
                **common,
                "id": "cross-project",
                "repository_id": "other-project",
                "title": "Other project parser secret",
                "content": "Use the incompatible OtherResult type. CROSS-PROJECT-CANARY",
                "expectation": "cross_project_guard",
                "canary": "CROSS-PROJECT-CANARY",
            }
        ),
    ]


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in result.content:
        text_value = getattr(item, "text", None)
        if isinstance(text_value, str):
            value = json.loads(text_value)
            if isinstance(value, dict):
                return value
    raise AssertionError("MCP result did not contain a JSON object")


async def _call_context(runtime: MemoryRuntime) -> dict[str, Any]:
    config = json.loads(runtime.config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"]["benchmark_memory"]
    parameters = StdioServerParameters(
        command=server["command"],
        args=server["args"],
        env=server["env"],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert tools == {"memory_context", "memory_search"}
        return _payload(
            await session.call_tool(
                "memory_context",
                arguments={
                    "task": "parser explicit result decision",
                    "repo": "project",
                },
            )
        )


@pytest.mark.asyncio
async def test_three_memory_conditions_use_real_tools_and_temporal_scope(tmp_path: Path) -> None:
    builder = MemoryRuntimeBuilder()
    task = _task()
    seeds = _seeds()
    baseline = builder.prepare(
        ExperimentCondition.NO_MEMORY,
        task,
        seeds,
        tmp_path / "baseline",
    )
    assert json.loads(baseline.config_path.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert builder.validate_usage(baseline).valid is True

    flat = builder.prepare(
        ExperimentCondition.FLAT_MEMORY,
        task,
        seeds,
        tmp_path / "flat",
    )
    flat_result = await _call_context(flat)
    assert flat_result["ok"] is True
    assert "explicit Result" in flat_result["result"]["text"]
    assert "raw string" in flat_result["result"]["text"]
    assert "OtherResult" in flat_result["result"]["text"]
    flat_usage = builder.validate_usage(flat)
    assert flat_usage.valid is True
    assert flat_usage.tool_calls == 1
    assert set(flat_usage.selected_seed_ids) == {"helpful", "stale", "cross-project"}

    memoryos = builder.prepare(
        ExperimentCondition.MEMORYOS,
        task,
        seeds,
        tmp_path / "memoryos",
    )
    assert "explicit Result object" not in memoryos.config_path.read_text(encoding="utf-8")
    memoryos_result = await _call_context(memoryos)
    assert memoryos_result["ok"] is True
    context_text = memoryos_result["result"]["text"]
    assert "explicit Result" in context_text
    assert "raw string" not in context_text
    assert "OtherResult" not in context_text
    memoryos_usage = builder.validate_usage(memoryos)
    assert memoryos_usage.valid is True
    assert memoryos_usage.tool_calls == 1
    assert memoryos_usage.retrieval_runs == 1
    assert memoryos_usage.selected_seed_ids == ("helpful",)

    assert memoryos.data_dir is not None
    database = Database(settings_for(memoryos.data_dir))
    with database.session() as session:
        statuses = {
            str(row.metadata_json["benchmark_seed_id"]): row.status.value
            for row in session.scalars(select(MemoryRow))
        }
    database.close()
    assert statuses["helpful"] == "active"
    assert statuses["stale"] == "expired"


@pytest.mark.asyncio
async def test_failed_scope_call_does_not_satisfy_usage_gate(tmp_path: Path) -> None:
    builder = MemoryRuntimeBuilder()
    runtime = builder.prepare(
        ExperimentCondition.FLAT_MEMORY,
        _task(),
        _seeds(),
        tmp_path / "flat-denied",
    )
    config = json.loads(runtime.config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"]["benchmark_memory"]
    parameters = StdioServerParameters(
        command=server["command"], args=server["args"], env=server["env"]
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = _payload(
            await session.call_tool(
                "memory_context",
                arguments={"task": "parser", "repo": "wrong-project"},
            )
        )
    assert result["ok"] is False
    evidence = builder.validate_usage(runtime)
    assert evidence.valid is False
    assert "no successful" in evidence.errors[0]
