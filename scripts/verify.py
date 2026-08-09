"""Run every MemoryOS V1 quality, acceptance, build, and production gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
REPORT = ROOT / "docs" / "verification" / "verify-summary.json"


def _pnpm() -> tuple[str, dict[str, str]]:
    environment = os.environ.copy()
    dependencies = (
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies"
    )
    bundled = dependencies / "bin" / "fallback" / "pnpm.cmd"
    node_bin = dependencies / "node" / "bin"
    if shutil.which("node.exe", path=environment.get("PATH")) is None and node_bin.is_dir():
        environment["PATH"] = os.pathsep.join([str(node_bin), environment.get("PATH", "")])
    configured = environment.get("MEMORYOS_PNPM")
    discovered = shutil.which("pnpm.cmd", path=environment["PATH"]) or shutil.which(
        "pnpm", path=environment["PATH"]
    )
    if configured:
        return configured, environment
    if discovered:
        return discovered, environment
    if bundled.is_file() and node_bin.is_dir():
        environment["PATH"] = os.pathsep.join(
            [str(node_bin), str(bundled.parent), environment.get("PATH", "")]
        )
        return str(bundled), environment
    raise SystemExit("pnpm is unavailable; install pnpm or set MEMORYOS_PNPM")


def _write_report(result: str, steps: list[dict[str, Any]]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "result": result,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "steps": steps,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def run(
    label: str,
    command: list[str],
    steps: list[dict[str, Any]],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    print(f"\n== {label} ==", flush=True)
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=cwd, env=environment, check=False)
    elapsed = round(time.perf_counter() - started, 3)
    steps.append(
        {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "elapsed_seconds": elapsed,
        }
    )
    if completed.returncode:
        _write_report("FAIL", steps)
        raise SystemExit(completed.returncode)


def main() -> None:
    pnpm, frontend_environment = _pnpm()
    python = sys.executable
    steps: list[dict[str, Any]] = []
    run("Backend import", [python, "-c", "import memoryos; print(memoryos.__version__)"], steps)
    run("Ruff", [python, "-m", "ruff", "check", "memoryos", "tests", "scripts"], steps)
    run(
        "Ruff format",
        [python, "-m", "ruff", "format", "--check", "memoryos", "tests", "scripts"],
        steps,
    )
    run("Mypy", [python, "-m", "mypy", "memoryos"], steps)
    run("Pytest", [python, "-m", "pytest", "-q"], steps)
    run("Frontend typecheck", [pnpm, "typecheck"], steps, cwd=WEB, environment=frontend_environment)
    run("Frontend lint", [pnpm, "lint"], steps, cwd=WEB, environment=frontend_environment)
    run("Frontend unit tests", [pnpm, "test"], steps, cwd=WEB, environment=frontend_environment)
    run(
        "Frontend production build",
        [pnpm, "build"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
    )
    run("Playwright E2E", [pnpm, "test:e2e"], steps, cwd=WEB, environment=frontend_environment)
    run(
        "10,000-record FTS performance",
        [
            python,
            "scripts/benchmark_search.py",
            "--records",
            "10000",
            "--rounds",
            "7",
            "--output",
            "docs/verification/performance.json",
        ],
        steps,
    )
    run(
        "Backend wheel",
        [
            python,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            "build/wheel",
        ],
        steps,
    )
    run("Windows PyInstaller", [python, "scripts/build_windows.py"], steps)
    run(
        "Packaged production smoke",
        [
            python,
            "scripts/production_smoke.py",
            "--distribution",
            "release/MemoryOS",
            "--output",
            "docs/verification/package-smoke.json",
        ],
        steps,
    )
    _write_report("PASS", steps)
    print(f"\nMemoryOS V1 verification passed ({len(steps)} gates).")
    print(REPORT)


if __name__ == "__main__":
    main()
