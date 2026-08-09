from __future__ import annotations

import json
import socket
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from memoryos.api import create_app
from memoryos.backup import BackupService
from memoryos.config import MemoryOSSettings, settings_for
from memoryos.db import Database
from memoryos.doctor import run_doctor
from memoryos.domain.schemas import (
    ConflictStrategy,
    CreatedBy,
    MemoryCreate,
    MemoryStatus,
    MemoryType,
    ScopeType,
    SearchRequest,
    SourceCreate,
    SourceType,
)
from memoryos.engine import MemoryService
from memoryos.mcp_server.server import run_mcp

app = typer.Typer(no_args_is_help=True, help="Local-first memory for coding agents")
console = Console()


@dataclass
class Runtime:
    settings: MemoryOSSettings

    def components(self) -> tuple[Database, MemoryService, BackupService]:
        database = Database(self.settings)
        database.initialize()
        return (
            database,
            MemoryService(database, self.settings),
            BackupService(database, self.settings),
        )


@app.callback()
def root(
    context: typer.Context,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", envvar="MEMORYOS_HOME", help="Local MemoryOS data directory"),
    ] = None,
) -> None:
    context.obj = Runtime(settings_for(data_dir))


def _runtime(context: typer.Context) -> Runtime:
    value = context.obj
    if not isinstance(value, Runtime):
        raise typer.Exit(2)
    return value


def _select_port(host: str, requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@app.command()
def serve(
    context: typer.Context,
    host: Annotated[str, typer.Option(help="Loopback bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port; 0 selects an available port")] = 0,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Start the local HTTP API and bundled management UI."""
    runtime = _runtime(context)
    selected = _select_port(host, port)
    runtime.settings.host = host
    runtime.settings.port = selected
    runtime.settings.ensure_directories()
    runtime.settings.runtime_path.write_text(
        json.dumps({"host": host, "port": selected}), encoding="utf-8"
    )
    url = f"http://{host}:{selected}/"
    console.print(f"MEMORYOS_PORT={selected}")
    console.print(f"MemoryOS listening at {url}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(runtime.settings), host=host, port=selected, log_level="info")


@app.command()
def mcp(context: typer.Context) -> None:
    """Run the MCP server over standard input/output."""
    run_mcp(_runtime(context).settings)


@app.command()
def status(
    context: typer.Context,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    database, service, _ = _runtime(context).components()
    try:
        result = service.status()
        if json_output:
            console.print_json(data=result)
        else:
            table = Table(title="MemoryOS status")
            table.add_column("Field")
            table.add_column("Value")
            table.add_row("Database", result["database"])
            table.add_row("Schema", result["schema_version"])
            table.add_row("Mode", result["mode"])
            table.add_row("Memories", str(sum(result["counts"].values())))
            table.add_row("Conflicts", str(result["conflicts"]))
            console.print(table)
    finally:
        database.close()


@app.command()
def doctor(
    context: typer.Context,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    database, _, _ = _runtime(context).components()
    try:
        result = run_doctor(database, _runtime(context).settings)
        if json_output:
            console.print_json(data=result)
        else:
            table = Table(title=f"MemoryOS doctor: {result['overall']}")
            table.add_column("Check")
            table.add_column("Result")
            table.add_column("Detail")
            for check in result["checks"]:
                table.add_row(check["name"], check["status"], check["detail"])
            console.print(table)
        if result["overall"] == "FAIL":
            raise typer.Exit(1)
    finally:
        database.close()


@app.command()
def propose(
    context: typer.Context,
    title: Annotated[str, typer.Option(prompt=True)],
    content: Annotated[str, typer.Option(prompt=True)],
    repo: Annotated[str, typer.Option("--repo")],
    key: Annotated[str | None, typer.Option()] = None,
    category: Annotated[str, typer.Option()] = "note",
    memory_type: Annotated[MemoryType, typer.Option()] = MemoryType.PROJECT,
    source_ref: Annotated[str, typer.Option()] = "cli",
) -> None:
    database, service, _ = _runtime(context).components()
    try:
        result = service.propose(
            MemoryCreate(
                scope_type=ScopeType.REPOSITORY,
                scope_key=repo,
                memory_type=memory_type,
                category=category,
                key=key,
                title=title,
                content=content,
                created_by=CreatedBy.MANUAL,
                source=SourceCreate(
                    source_type=SourceType.MANUAL,
                    source_ref=source_ref,
                    excerpt=content,
                ),
            ),
            actor="cli",
        )
        console.print_json(data=result)
    finally:
        database.close()


@app.command()
def search(
    context: typer.Context,
    query: str,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
) -> None:
    database, service, _ = _runtime(context).components()
    try:
        console.print_json(
            data=service.search(SearchRequest(query=query, scope_key=repo, limit=limit))
        )
    finally:
        database.close()


@app.command("list")
def list_memories(
    context: typer.Context,
    memory_status: Annotated[MemoryStatus | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    database, service, _ = _runtime(context).components()
    try:
        console.print_json(
            data=service.search(
                SearchRequest(query="", status=memory_status, include_history=True, limit=limit)
            )
        )
    finally:
        database.close()


@app.command()
def confirm(
    context: typer.Context,
    memory_id: str,
    strategy: Annotated[ConflictStrategy | None, typer.Option()] = None,
) -> None:
    database, service, _ = _runtime(context).components()
    try:
        console.print_json(data=service.confirm(memory_id, strategy=strategy, actor="cli"))
    finally:
        database.close()


@app.command()
def forget(context: typer.Context, memory_id: str) -> None:
    database, service, _ = _runtime(context).components()
    try:
        console.print_json(data=service.forget(memory_id, actor="cli"))
    finally:
        database.close()


@app.command("export")
def export_data(context: typer.Context, output: Annotated[Path, typer.Option("--output")]) -> None:
    database, _, backup = _runtime(context).components()
    try:
        console.print(str(backup.export_jsonl(output)))
    finally:
        database.close()


@app.command("import")
def import_data(context: typer.Context, archive: Path) -> None:
    database, _, backup = _runtime(context).components()
    try:
        console.print(f"Imported {backup.import_jsonl(archive)} records")
    finally:
        database.close()


@app.command()
def backup(context: typer.Context, output: Annotated[Path | None, typer.Option()] = None) -> None:
    database, _, backup_service = _runtime(context).components()
    try:
        console.print(str(backup_service.create_backup(output)))
    finally:
        database.close()


@app.command()
def restore(context: typer.Context, archive: Path) -> None:
    database, _, backup_service = _runtime(context).components()
    try:
        safety = backup_service.restore(archive)
        console.print(f"Restore complete; safety backup: {safety}")
    finally:
        database.close()


def main() -> None:
    app()
