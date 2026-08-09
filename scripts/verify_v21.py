"""Run the complete MemoryOS V2.1 A33-A52 release verification on main."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


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


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _write_report(
    output: Path,
    result: str,
    steps: list[dict[str, Any]],
    *,
    started_commit: str,
    dirty_before_run: bool,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema": "memoryos-v2.1-verification@1",
                "generated_at": datetime.now(UTC).isoformat(),
                "result": result,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "branch": _git("branch", "--show-current"),
                "started_commit": started_commit,
                "git_dirty_before_run": dirty_before_run,
                "steps": steps,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run(
    label: str,
    command: list[str],
    steps: list[dict[str, Any]],
    *,
    output: Path,
    started_commit: str,
    dirty_before_run: bool,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    print(f"\n== {label} ==", flush=True)
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=cwd, env=environment, check=False)
    steps.append(
        {
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    )
    _write_report(
        output,
        "RUNNING" if completed.returncode == 0 else "FAIL",
        steps,
        started_commit=started_commit,
        dirty_before_run=dirty_before_run,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the complete V2.1 release verification")
    parser.add_argument("--scratch-dir", type=Path, default=ROOT / "build" / "verification-v2.1")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "verification" / "v2.1" / "verify-summary.json",
    )
    args = parser.parse_args()
    scratch = args.scratch_dir.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    progress_output = scratch / "verify-progress.json"
    pnpm, frontend_environment = _pnpm()
    python = sys.executable
    steps: list[dict[str, Any]] = []
    started_commit = _git("rev-parse", "HEAD")
    dirty_before_run = bool(_git("status", "--porcelain"))
    common = {
        # Keep progress outside the tracked release-evidence directory. The
        # merged-main gate intentionally requires a clean worktree; publishing
        # the final report before that gate would make the verifier invalidate
        # its own run.
        "output": progress_output,
        "started_commit": started_commit,
        "dirty_before_run": dirty_before_run,
    }

    run(
        "Backend import",
        [python, "-c", "import memoryos; assert memoryos.__version__ == '2.1.0'"],
        steps,
        **common,
    )
    run("Ruff", [python, "-m", "ruff", "check", "memoryos", "tests", "scripts"], steps, **common)
    run(
        "Ruff format",
        [python, "-m", "ruff", "format", "--check", "memoryos", "tests", "scripts"],
        steps,
        **common,
    )
    run("Mypy", [python, "-m", "mypy", "memoryos"], steps, **common)
    run("Backend pytest", [python, "-m", "pytest", "-q"], steps, **common)
    run(
        "V2 regression MemoryBench",
        [python, "scripts/memorybench_v2.py", "--output-dir", str(scratch / "v2")],
        steps,
        **common,
    )
    run(
        "Blind CodingMemoryBench V2.1",
        [python, "scripts/coding_memory_bench.py", "--output-dir", str(scratch / "v2.1")],
        steps,
        **common,
    )
    run(
        "Paired real-agent protocol or explicit blocker",
        [
            python,
            "scripts/agent_ab_v21.py",
            "--tasks",
            "50",
            "--output",
            str(scratch / "v2.1" / "agent-ab.json"),
        ],
        steps,
        **common,
    )
    run(
        "100K full retrieval/context pipeline",
        [
            python,
            "scripts/benchmark_v21_pipeline.py",
            "--records",
            "100000",
            "--rounds",
            "25",
            "--output",
            str(scratch / "v2.1" / "full-pipeline-performance.json"),
        ],
        steps,
        **common,
    )
    run(
        "Frontend typecheck",
        [pnpm, "typecheck"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
        **common,
    )
    run(
        "Frontend lint",
        [pnpm, "lint"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
        **common,
    )
    run(
        "Frontend unit tests",
        [pnpm, "test"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
        **common,
    )
    run(
        "Frontend production build",
        [pnpm, "build"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
        **common,
    )
    run(
        "Playwright E2E",
        [pnpm, "test:e2e"],
        steps,
        cwd=WEB,
        environment=frontend_environment,
        **common,
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
        **common,
    )
    run("Windows PyInstaller", [python, "scripts/build_windows.py"], steps, **common)
    package_report = scratch / "package-smoke.json"
    run(
        "Packaged V1-to-V2.1 production smoke",
        [
            python,
            "scripts/production_smoke.py",
            "--distribution",
            "release/MemoryOS",
            "--output",
            str(package_report),
        ],
        steps,
        **common,
    )
    main_report = scratch / "v2.1" / "main-release-smoke.json"
    run(
        "Merged-main release smoke",
        [
            python,
            "scripts/main_release_smoke.py",
            "--distribution",
            "release/MemoryOS",
            "--output",
            str(main_report),
        ],
        steps,
        **common,
    )
    run(
        "A33-A52 evidence manifest",
        [
            python,
            "scripts/acceptance_v21.py",
            "--benchmark-report",
            str(scratch / "v2.1" / "coding-memory-bench.json"),
            "--performance-report",
            str(scratch / "v2.1" / "full-pipeline-performance.json"),
            "--agent-report",
            str(scratch / "v2.1" / "agent-ab.json"),
            "--package-report",
            str(package_report),
            "--main-smoke-report",
            str(main_report),
            "--output",
            str(scratch / "v2.1" / "acceptance-summary.json"),
        ],
        steps,
        **common,
    )
    _write_report(
        output,
        "PASS",
        steps,
        started_commit=started_commit,
        dirty_before_run=dirty_before_run,
    )
    print(f"\nMemoryOS V2.1 verification passed ({len(steps)} gates).")
    print(output)


if __name__ == "__main__":
    main()
