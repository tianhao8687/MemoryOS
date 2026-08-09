from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memoryos.api import create_app
from memoryos.config import settings_for
from memoryos.security.token import TokenManager


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in result.content:
        text_value = getattr(item, "text", None)
        if text_value:
            decoded = json.loads(text_value)
            if isinstance(decoded, dict):
                return decoded
    raise AssertionError("MCP tool returned no structured JSON")


@pytest.mark.asyncio
@pytest.mark.acceptance
async def test_real_stdio_cross_client_persistence(tmp_path: Path) -> None:
    data_dir = tmp_path / "mcp-data"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "memoryos.mcp_server.server", "--data-dir", str(data_dir)],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tool_names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "memory_context",
            "memory_search",
            "memory_propose",
            "memory_confirm",
            "memory_forget",
            "memory_history",
            "memory_explain",
            "memory_current_truth",
            "memory_feedback",
            "memory_consolidate",
            "memory_refresh",
            "memory_debug_context",
        }.issubset(tool_names)
        assert len(tool_names) == 12
        proposed = _payload(
            await session.call_tool(
                "memory_propose",
                arguments={
                    "title": "Use FastAPI",
                    "content": "Use FastAPI for authentication APIs.",
                    "scope_type": "repository",
                    "scope_key": "mcp-repo",
                    "memory_type": "project",
                    "category": "decision",
                    "key": "architecture.backend.framework",
                    "source_type": "agent",
                    "source_ref": "mcp:integration-test",
                    "source_excerpt": "Use FastAPI for authentication APIs.",
                },
            )
        )
        memory_id = proposed["result"]["id"]
        confirmed = _payload(
            await session.call_tool("memory_confirm", arguments={"memory_id": memory_id})
        )
        assert confirmed["result"]["status"] == "active"
        context = _payload(
            await session.call_tool(
                "memory_context",
                arguments={"task": "authentication API", "repo": "mcp-repo"},
            )
        )
        assert "FastAPI" in context["result"]["text"]

    settings = settings_for(data_dir)
    token = TokenManager(settings.token_path).get_or_create()
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/memories", params={"q": "FastAPI"})
        assert response.json()["total"] == 1
        assert client.get(f"/api/memories/{memory_id}/explain").status_code == 200
        assert token
