from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memoryos.config import settings_for
from memoryos.context.token_meter import UnicodeHeuristicTokenCounter
from memoryos.mcp_server.server import create_mcp_server
from memoryos.mcp_server.tool_registry import (
    ALL_TOOLS,
    CONTEXT_TOOLS,
    CORE_TOOLS,
    DEBUG_TOOLS,
    GOVERNANCE_TOOLS,
    ToolProfile,
    server_schema_snapshot,
)


def _tool_payload(value: Any) -> dict[str, Any]:
    assert isinstance(value, tuple)
    assert len(value) == 2
    assert isinstance(value[1], dict)
    return value[1]


@pytest.mark.asyncio
@pytest.mark.v23
@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (ToolProfile.ALL, ALL_TOOLS),
        (ToolProfile.CORE, CORE_TOOLS),
        (ToolProfile.CONTEXT, CONTEXT_TOOLS),
        (ToolProfile.GOVERNANCE, GOVERNANCE_TOOLS),
        (ToolProfile.DEBUG, DEBUG_TOOLS),
    ],
)
async def test_mcp_profiles_expose_fixed_deterministic_tool_sets(
    tmp_path: Path,
    profile: ToolProfile,
    expected: tuple[str, ...],
) -> None:
    server = create_mcp_server(settings_for(tmp_path / profile.value), profile)

    tools = await server.list_tools()

    assert tuple(tool.name for tool in tools) == expected
    assert len(tools) == len(expected)


@pytest.mark.asyncio
@pytest.mark.v23
async def test_mcp_profile_uses_settings_without_a_cli_override(tmp_path: Path) -> None:
    server = create_mcp_server(settings_for(tmp_path / "configured-core", mcp_tool_profile="core"))

    tools = await server.list_tools()

    assert tuple(tool.name for tool in tools) == CORE_TOOLS


@pytest.mark.asyncio
@pytest.mark.v23
async def test_core_keeps_write_confirmation_loop_without_governance_or_debug(
    tmp_path: Path,
) -> None:
    server = create_mcp_server(settings_for(tmp_path / "core"), ToolProfile.CORE)
    names = {tool.name for tool in await server.list_tools()}

    assert {"memory_propose", "memory_confirm"}.issubset(names)
    assert "memory_forget" not in names
    assert "memory_consolidate" not in names
    assert "memory_debug_context" not in names
    assert len(names) < 10


@pytest.mark.asyncio
@pytest.mark.v23
async def test_context_profile_exposes_only_compiled_context(tmp_path: Path) -> None:
    server = create_mcp_server(settings_for(tmp_path / "context", mcp_tool_profile="context"))

    tools = await server.list_tools()

    assert tuple(tool.name for tool in tools) == CONTEXT_TOOLS

    counter = UnicodeHeuristicTokenCounter()
    context_snapshot = await server_schema_snapshot(
        server,
        profile=ToolProfile.CONTEXT,
        counter=counter,
    )
    core_server = create_mcp_server(settings_for(tmp_path / "core-schema"), ToolProfile.CORE)
    core_snapshot = await server_schema_snapshot(
        core_server,
        profile=ToolProfile.CORE,
        counter=counter,
    )
    assert context_snapshot["estimated_schema_tokens"] < core_snapshot["estimated_schema_tokens"]


@pytest.mark.asyncio
@pytest.mark.v23
async def test_tool_schema_snapshot_hash_and_estimate_are_stable(tmp_path: Path) -> None:
    counter = UnicodeHeuristicTokenCounter()
    first_server = create_mcp_server(settings_for(tmp_path / "one"), ToolProfile.CORE)
    second_server = create_mcp_server(settings_for(tmp_path / "two"), ToolProfile.CORE)

    first = await server_schema_snapshot(
        first_server,
        profile=ToolProfile.CORE,
        counter=counter,
    )
    second = await server_schema_snapshot(
        second_server,
        profile=ToolProfile.CORE,
        counter=counter,
    )

    assert first == second
    assert first["tool_names"] == list(CORE_TOOLS)
    assert len(first["schema_sha256"]) == 64
    assert first["estimated_schema_tokens"] > 0
    assert first["counter_kind"] == "estimated"


@pytest.mark.asyncio
@pytest.mark.v23
async def test_mcp_context_explain_hash_conflict_and_debug_error_contract(
    tmp_path: Path,
) -> None:
    server = create_mcp_server(
        settings_for(tmp_path / "msc-all", context_compiler_mode="msc"),
        ToolProfile.ALL,
    )
    proposed = _tool_payload(
        await server.call_tool(
            "memory_propose",
            {
                "title": "Refund entry decision",
                "content": "RefundService is the only refund entry point.",
                "scope_type": "repository",
                "scope_key": "repo-a",
                "memory_type": "project",
                "category": "decision",
                "source_type": "agent",
                "source_ref": "mcp:v23",
                "source_excerpt": "RefundService is the only refund entry point.",
            },
        )
    )
    memory_id = proposed["result"]["id"]
    confirmed = _tool_payload(await server.call_tool("memory_confirm", {"memory_id": memory_id}))
    assert confirmed["ok"] is True

    context = _tool_payload(
        await server.call_tool(
            "memory_context",
            {
                "task": "find the RefundService entry decision",
                "repo": "repo-a",
                "budget_tokens": 5000,
            },
        )
    )["result"]
    assert context["schema_version"] == "2.3"
    assert all(field not in context for field in ("sections", "manifest", "query_plan", "debug"))

    conflict = _tool_payload(
        await server.call_tool(
            "memory_explain",
            {
                "memory_id": memory_id,
                "expected_atom_sha256": "0" * 64,
            },
        )
    )
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "CONTEXT_CHANGED"
    assert conflict["error"]["details"]["refresh_required"] is True

    legacy_explain = _tool_payload(
        await server.call_tool("memory_explain", {"memory_id": memory_id})
    )
    assert legacy_explain["ok"] is True
    assert legacy_explain["result"]["memory"]["id"] == memory_id

    debug = _tool_payload(
        await server.call_tool(
            "memory_debug_context",
            {"retrieval_run_id": context["retrieval_run_id"]},
        )
    )
    assert debug["ok"] is True
    assert debug["result"]["context_diagnostics"]["query_plan"]
