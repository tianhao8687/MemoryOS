"""Run the packaged Windows application through its production smoke workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(executable: Path, data_dir: Path, port: int) -> subprocess.Popen[str]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        [
            str(executable),
            "--data-dir",
            str(data_dir),
            "serve",
            "--port",
            str(port),
            "--no-open",
        ],
        cwd=executable.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )


def _stop(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    output, _ = process.communicate(timeout=5)
    return output


def _wait_for_health(process: subprocess.Popen[str], base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate(timeout=5)
            raise RuntimeError(f"packaged server exited early ({process.returncode}):\n{output}")
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=1, trust_env=False)
            if response.status_code == 200:
                payload = response.json()
                if payload.get("ok") is True:
                    return payload
        except (httpx.HTTPError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"packaged server health timeout: {last_error}")


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in result.content:
        value = getattr(item, "text", None)
        if value:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
    raise RuntimeError("packaged MCP tool returned no structured JSON")


async def _mcp_write(executable: Path, data_dir: Path) -> str:
    parameters = StdioServerParameters(
        command=str(executable), args=["--data-dir", str(data_dir), "mcp"]
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        required = {
            "memory_context",
            "memory_search",
            "memory_propose",
            "memory_confirm",
            "memory_forget",
            "memory_history",
            "memory_explain",
        }
        if not required.issubset(names):
            raise RuntimeError(f"packaged MCP tools are incomplete: {sorted(names)}")
        proposed = _tool_payload(
            await session.call_tool(
                "memory_propose",
                arguments={
                    "title": "Package smoke persistence",
                    "content": "Package smoke memory survives a packaged process restart.",
                    "scope_type": "repository",
                    "scope_key": "package-repo",
                    "memory_type": "project",
                    "category": "decision",
                    "key": "release.package.smoke",
                    "source_type": "agent",
                    "source_ref": "package-smoke:mcp",
                    "source_excerpt": "Package smoke persistence",
                },
            )
        )
        memory_id = str(proposed["result"]["id"])
        confirmed = _tool_payload(
            await session.call_tool("memory_confirm", arguments={"memory_id": memory_id})
        )
        if confirmed["result"]["status"] != "active":
            raise RuntimeError("packaged MCP confirmation did not activate memory")
        context = _tool_payload(
            await session.call_tool(
                "memory_context",
                arguments={"task": "package persistence", "repo": "package-repo"},
            )
        )
        if "Package smoke" not in context["result"]["text"]:
            raise RuntimeError("packaged MCP context did not return the confirmed memory")
        return memory_id


def _assert_http_state(base_url: str, memory_id: str | None = None) -> None:
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        root = client.get("/")
        if root.status_code != 200 or '<div id="root"></div>' not in root.text:
            raise RuntimeError("bundled management UI is unavailable")
        if memory_id is not None:
            search = client.get("/api/memories", params={"q": "Package smoke"})
            if search.status_code != 200 or search.json().get("total") != 1:
                raise RuntimeError("HTTP client cannot read the MCP-created memory")
            explain = client.get(f"/api/memories/{memory_id}/explain")
            if explain.status_code != 200 or not explain.json().get("sources"):
                raise RuntimeError("packaged explain endpoint lacks provenance")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distribution", type=Path, default=ROOT / "release" / "MemoryOS")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "verification" / "package-smoke.json",
    )
    args = parser.parse_args()
    distribution = args.distribution.expanduser().resolve()
    source_executable = distribution / "MemoryOS.exe"
    if not source_executable.is_file():
        raise SystemExit(f"packaged executable is missing: {source_executable}")
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="memoryos-package-smoke-") as directory:
        clean_root = Path(directory) / "clean path"
        clean_distribution = clean_root / "MemoryOS"
        shutil.copytree(distribution, clean_distribution)
        executable = clean_distribution / "MemoryOS.exe"
        data_dir = clean_root / "user data"
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        first = _start(executable, data_dir, port)
        try:
            first_health = _wait_for_health(first, base_url)
            _assert_http_state(base_url)
            memory_id = asyncio.run(_mcp_write(executable, data_dir))
            _assert_http_state(base_url, memory_id)
        finally:
            first_output = _stop(first)
        second = _start(executable, data_dir, port)
        try:
            second_health = _wait_for_health(second, base_url)
            _assert_http_state(base_url, memory_id)
        finally:
            second_output = _stop(second)
        cli = subprocess.run(
            [str(executable), "--data-dir", str(data_dir), "status", "--json"],
            cwd=executable.parent,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if cli.returncode:
            raise RuntimeError(f"packaged CLI status failed:\n{cli.stdout}\n{cli.stderr}")
    report = {
        "result": "PASS",
        "distribution": str(distribution),
        "clean_path_copy": True,
        "first_health": first_health,
        "second_health": second_health,
        "mcp_tools": 7,
        "memory_id": memory_id,
        "restart_persistence": True,
        "ui_health": True,
        "cli_status": True,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "server_output_lines": len((first_output + second_output).splitlines()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
