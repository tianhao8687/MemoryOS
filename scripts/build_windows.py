"""Build the Windows MemoryOS distribution with bundled UI and migrations."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
WORK_DIR = ROOT / "build" / "pyinstaller"
SPEC_DIR = ROOT / "build"


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Windows packaging must run on a Windows host")
    web_dist = ROOT / "web" / "dist"
    migrations = ROOT / "memoryos" / "db" / "migrations"
    benchmark_report = ROOT / "docs" / "verification" / "v2" / "memorybench-report.json"
    entrypoint = ROOT / "memoryos" / "__main__.py"
    if not (web_dist / "index.html").is_file():
        raise SystemExit("web/dist is missing; run the frontend production build first")
    if not benchmark_report.is_file():
        raise SystemExit("MemoryBench report is missing; run scripts/memorybench_v2.py first")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        "MemoryOS",
        "--distpath",
        str(RELEASE_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(SPEC_DIR),
        "--paths",
        str(ROOT),
        "--add-data",
        f"{web_dist}{os.pathsep}web_dist",
        "--add-data",
        f"{migrations}{os.pathsep}memoryos/db/migrations",
        "--add-data",
        f"{benchmark_report}{os.pathsep}verification",
        "--collect-all",
        "mcp",
        "--collect-all",
        "tree_sitter_language_pack",
        "--collect-submodules",
        "memoryos",
        str(entrypoint),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    executable = RELEASE_DIR / "MemoryOS" / "MemoryOS.exe"
    if not executable.is_file():
        raise SystemExit(f"PyInstaller succeeded but the executable is missing: {executable}")
    print(executable)


if __name__ == "__main__":
    main()
