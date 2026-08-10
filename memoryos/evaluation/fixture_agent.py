from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _mcp_url(config_path: Path) -> str | None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("mcpServers", {})
    if not isinstance(servers, dict) or not servers:
        return None
    server = servers.get("benchmark_memory")
    if not isinstance(server, dict) or not isinstance(server.get("url"), str):
        raise ValueError("fixture agent requires the benchmark streamable-HTTP MCP config")
    return str(server["url"])


async def _retrieve_context(url: str, prompt: str, repository: str) -> int:
    async with (
        streamablehttp_client(url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "memory_context",
            arguments={"task": prompt[:2000], "repo": repository},
        )
        if getattr(result, "isError", False):
            raise RuntimeError("memory_context returned an MCP error")
    return 1


def _markupsafe_deprecation(workspace: Path) -> None:
    source_path = workspace / "src" / "markupsafe" / "__init__.py"
    source = source_path.read_text(encoding="utf-8")
    old = "            stacklevel=2,\n        )\n        return importlib.metadata.version"
    new = (
        "            DeprecationWarning,\n"
        "            stacklevel=2,\n"
        "        )\n"
        "        return importlib.metadata.version"
    )
    if old not in source:
        raise ValueError("MarkupSafe fixture source does not match the pinned base commit")
    source_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    changes_path = workspace / "CHANGES.rst"
    changes = changes_path.read_text(encoding="utf-8")
    heading = "Unreleased\n\n"
    entry = (
        "-   ``__version__`` raises ``DeprecationWarning`` instead of ``UserWarning``.\n"
        "    :issue:`487`\n\n"
    )
    if heading not in changes:
        raise ValueError("MarkupSafe fixture changelog does not match the pinned base commit")
    changes_path.write_text(changes.replace(heading, heading + entry, 1), encoding="utf-8")


def _write_result(path: Path, *, status: str, tool_calls: int, message: str | None) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "tool_calls": tool_calls,
        "message": message,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic infrastructure-only agent for real-workload smoke tests."
    )
    parser.add_argument("--strategy", choices=["markupsafe-deprecation"], required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args()
    tool_calls = 0
    try:
        prompt = arguments.prompt.read_text(encoding="utf-8")
        repository_line = prompt.splitlines()[0]
        repository = repository_line.removeprefix("Repository scope:").strip()
        if not repository:
            raise ValueError("benchmark prompt has no repository scope")
        url = _mcp_url(arguments.mcp_config)
        tool_calls = asyncio.run(_retrieve_context(url, prompt, repository)) if url else 0
        if arguments.strategy == "markupsafe-deprecation":
            _markupsafe_deprecation(arguments.workspace)
        _write_result(
            arguments.result,
            status="completed",
            tool_calls=tool_calls,
            message="deterministic infrastructure fixture completed",
        )
    except Exception as exc:
        _write_result(
            arguments.result,
            status="failed",
            tool_calls=tool_calls,
            message=f"fixture failure: {type(exc).__name__}",
        )


if __name__ == "__main__":
    main()
