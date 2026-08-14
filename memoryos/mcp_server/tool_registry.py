from __future__ import annotations

import hashlib
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from mcp.server.fastmcp import FastMCP

from memoryos.context.token_meter import TokenCounter, canonical_json


class ToolProfile(StrEnum):
    ALL = "all"
    CORE = "core"
    GOVERNANCE = "governance"
    DEBUG = "debug"


ALL_TOOLS = (
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
)
CORE_TOOLS = (
    "memory_context",
    "memory_search",
    "memory_propose",
    "memory_confirm",
    "memory_explain",
    "memory_current_truth",
)
GOVERNANCE_TOOLS = (
    "memory_forget",
    "memory_feedback",
    "memory_consolidate",
    "memory_refresh",
)
DEBUG_TOOLS = (
    "memory_history",
    "memory_debug_context",
)
PROFILE_TOOLS = {
    ToolProfile.ALL: ALL_TOOLS,
    ToolProfile.CORE: CORE_TOOLS,
    ToolProfile.GOVERNANCE: GOVERNANCE_TOOLS,
    ToolProfile.DEBUG: DEBUG_TOOLS,
}


class ToolRegistry:
    """Startup-only registry; its profile cannot change after construction."""

    def __init__(self, mcp: FastMCP, profile: ToolProfile | str) -> None:
        self.mcp = mcp
        self.profile = ToolProfile(str(profile))
        self.names = PROFILE_TOOLS[self.profile]
        self._registered: list[str] = []

    def tool(self, *, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if name not in ALL_TOOLS:
            raise ValueError(f"unknown MemoryOS tool: {name}")

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            if name not in self.names:
                return function
            expected = self.names[len(self._registered)]
            if name != expected:
                raise RuntimeError(
                    f"tool registration order is not deterministic: expected {expected}, got {name}"
                )
            self.mcp.tool(name=name)(function)
            self._registered.append(name)
            return function

        return decorator

    def assert_complete(self) -> None:
        if tuple(self._registered) != self.names:
            missing = self.names[len(self._registered) :]
            raise RuntimeError(f"tool profile registration is incomplete: {missing}")


def schema_snapshot(
    *,
    profile: ToolProfile | str,
    tools: list[dict[str, Any]],
    counter: TokenCounter,
) -> dict[str, Any]:
    selected = ToolProfile(str(profile))
    canonical_tools = sorted(
        tools,
        key=lambda item: PROFILE_TOOLS[selected].index(str(item["name"])),
    )
    serialized = canonical_json(canonical_tools)
    return {
        "schema_version": "1.0",
        "profile": selected.value,
        "tool_names": list(PROFILE_TOOLS[selected]),
        "tools": canonical_tools,
        "schema_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "estimated_schema_tokens": counter.count_text(serialized),
        "counter_kind": counter.kind.value,
        "tokenizer_id": counter.tokenizer_id,
        "counter_version": counter.counter_version,
    }


async def server_schema_snapshot(
    mcp: FastMCP,
    *,
    profile: ToolProfile | str,
    counter: TokenCounter,
) -> dict[str, Any]:
    tools = [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in await mcp.list_tools()
    ]
    return schema_snapshot(profile=profile, tools=tools, counter=counter)


__all__ = [
    "ALL_TOOLS",
    "CORE_TOOLS",
    "DEBUG_TOOLS",
    "GOVERNANCE_TOOLS",
    "PROFILE_TOOLS",
    "ToolProfile",
    "ToolRegistry",
    "schema_snapshot",
    "server_schema_snapshot",
]
