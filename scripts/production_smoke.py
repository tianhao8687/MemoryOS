"""Run the packaged Windows application through its production smoke workflow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import httpx
from alembic import command
from alembic.config import Config
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

from memoryos.config import settings_for
from memoryos.db import Database

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
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return cast(dict[str, Any], payload)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"packaged server health timeout: {last_error}")


def _prepare_v1_database(data_dir: Path) -> str:
    """Create an actual 0001 database so the packaged app must perform the V2 upgrade."""

    database = Database(settings_for(data_dir))
    database.initialize()
    migrations = ROOT / "memoryos" / "db" / "migrations"
    config = Config()
    config.set_main_option("script_location", str(migrations))
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_initial")
    legacy_id = "00000000-0000-0000-0000-000000000001"
    source_id = "00000000-0000-0000-0000-000000000002"
    excerpt = "V1 packaged upgrade memory must survive migration."
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO memories (
                    id, scope_type, scope_key, memory_type, category, subject, key,
                    title, content, status, confidence, importance, valid_from, valid_to,
                    ttl_seconds, supersedes_id, created_at, updated_at, created_by,
                    sensitivity, metadata_json
                ) VALUES (
                    :id, 'repository', 'package-repo', 'project', 'decision',
                    'release migration', 'release.v1.upgrade', :title, :content, 'active',
                    0.9, 0.8, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, 'manual', 'normal', '{}'
                )
                """
            ),
            {"id": legacy_id, "title": "V1 upgrade persistence", "content": excerpt},
        )
        connection.execute(
            text(
                """
                INSERT INTO sources (
                    id, source_type, source_ref, captured_at, excerpt,
                    content_hash, metadata_json, created_at
                ) VALUES (
                    :id, 'import', 'package-smoke:v1', CURRENT_TIMESTAMP, :excerpt,
                    :content_hash, '{}', CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": source_id,
                "excerpt": excerpt,
                "content_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            },
        )
        connection.execute(
            text("INSERT INTO memory_sources (memory_id, source_id) VALUES (:memory, :source)"),
            {"memory": legacy_id, "source": source_id},
        )
    if database.schema_version() != "0001_initial":
        raise RuntimeError("failed to prepare the V1 migration fixture")
    database.close()
    return legacy_id


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(f"Git fixture command failed: {completed.stderr}")
    return completed.stdout.strip()


def _prepare_anchor_repository(clean_root: Path) -> Path:
    repository = clean_root / "tree-sitter repository"
    source = repository / "src" / "store.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'class Store:\n    def refresh(self) -> str:\n        return "fresh"\n',
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "config", "user.name", "MemoryOS Package Smoke")
    _git(repository, "config", "user.email", "memoryos@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "package tree-sitter anchor")
    return repository


def _run_packaged_json(
    executable: Path,
    data_dir: Path,
    *arguments: str,
) -> dict[str, Any]:
    completed = subprocess.run(
        [str(executable), "--data-dir", str(data_dir), *arguments],
        cwd=executable.parent,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(
            f"packaged CLI {' '.join(arguments)} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"packaged CLI returned invalid JSON: {completed.stdout}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("packaged CLI JSON must be an object")
    return cast(dict[str, Any], payload)


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


async def _mcp_write(executable: Path, data_dir: Path) -> tuple[str, int]:
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
            "memory_current_truth",
            "memory_feedback",
            "memory_consolidate",
            "memory_refresh",
            "memory_debug_context",
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
        return memory_id, len(names)


def _assert_http_state(
    base_url: str,
    token: str,
    memory_id: str | None = None,
    legacy_id: str | None = None,
) -> None:
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
    ) as client:
        root = client.get("/")
        if root.status_code != 200 or '<div id="root"></div>' not in root.text:
            raise RuntimeError("bundled management UI is unavailable")
        benchmark = client.get("/api/benchmarks/memorybench-v2")
        if (
            benchmark.status_code != 200
            or benchmark.json().get("schema") != "memorybench-v2-report@1"
        ):
            raise RuntimeError("bundled MemoryBench report is unavailable")
        coding_benchmark = client.get("/api/benchmarks/coding-memory-bench-v2.1")
        if (
            coding_benchmark.status_code != 200
            or coding_benchmark.json().get("schema") != "coding-memory-bench-v2.1@1"
        ):
            raise RuntimeError("bundled CodingMemoryBench fixture report is unavailable")
        doctor = client.get("/api/doctor")
        doctor_checks = {item.get("name"): item for item in doctor.json().get("checks", [])}
        if (
            doctor.status_code != 200
            or doctor_checks.get("sqlite_vec_runtime", {}).get("status") != "PASS"
        ):
            raise RuntimeError("packaged sqlite-vec runtime is unavailable")
        if legacy_id is not None:
            legacy = client.get("/api/memories", params={"q": "V1 upgrade persistence"})
            legacy_ids = {item["memory"]["id"] for item in legacy.json().get("items", [])}
            if legacy.status_code != 200 or legacy_id not in legacy_ids:
                raise RuntimeError("V1 memory did not survive the packaged V2 migration")
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
        legacy_id = _prepare_v1_database(data_dir)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        first = _start(executable, data_dir, port)
        try:
            first_health = _wait_for_health(first, base_url)
            token = (data_dir / "auth.token").read_text(encoding="utf-8").strip()
            _assert_http_state(base_url, token, legacy_id=legacy_id)
            with httpx.Client(
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
                trust_env=False,
            ) as client:
                first_status = client.get("/api/status").json()
            if first_status.get("schema_version") != "0004_anchor_observation_hardening":
                raise RuntimeError("packaged app did not migrate the V1 database to V2.2")
            memory_id, mcp_tool_count = asyncio.run(_mcp_write(executable, data_dir))
            anchor_repository = _prepare_anchor_repository(clean_root)
            anchor = _run_packaged_json(
                executable,
                data_dir,
                "anchor",
                memory_id,
                str(anchor_repository),
                "src/store.py",
                "--symbol-fqn",
                "Store.refresh",
            )
            if anchor.get("parser_backend") != "tree-sitter":
                raise RuntimeError("packaged Tree-sitter grammar did not load for a Python symbol")
            refreshed = _run_packaged_json(
                executable,
                data_dir,
                "refresh",
                memory_id,
                str(anchor_repository),
            )
            if refreshed.get("freshness") != "fresh":
                raise RuntimeError("packaged source anchor did not refresh as fresh")
            _assert_http_state(base_url, token, memory_id, legacy_id)
        finally:
            first_output = _stop(first)
        second = _start(executable, data_dir, port)
        try:
            second_health = _wait_for_health(second, base_url)
            _assert_http_state(base_url, token, memory_id, legacy_id)
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
        "mcp_tools": mcp_tool_count,
        "memory_id": memory_id,
        "legacy_memory_id": legacy_id,
        "v1_to_v2_migration": True,
        "v1_to_v21_migration": True,
        "v1_to_v22_migration": True,
        "schema_version": first_status["schema_version"],
        "memorybench_bundled": True,
        "coding_memory_bench_bundled": True,
        "sqlite_vec_bundled": True,
        "tree_sitter_bundled": anchor["parser_backend"] == "tree-sitter",
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
