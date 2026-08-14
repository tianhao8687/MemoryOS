"""Verify the packaged V2.2 release from a clean main checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


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


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object report: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the V2.2 merged-main release smoke")
    parser.add_argument("--distribution", type=Path, default=ROOT / "release" / "MemoryOS")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "verification" / "v2.2" / "main-release-smoke.json",
    )
    args = parser.parse_args()
    branch = _git("branch", "--show-current")
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    executable = args.distribution.resolve() / "MemoryOS.exe"
    if not executable.is_file():
        raise SystemExit(f"packaged executable is missing: {executable}")

    with tempfile.TemporaryDirectory(prefix="memoryos-main-smoke-") as directory:
        package_path = Path(directory) / "package-smoke.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "production_smoke.py"),
                "--distribution",
                str(args.distribution),
                "--output",
                str(package_path),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode:
            print(completed.stdout)
            print(completed.stderr, file=sys.stderr)
            return completed.returncode
        package = _read(package_path)

    package_passed = bool(
        package.get("result") == "PASS"
        and package.get("v1_to_v22_migration") is True
        and package.get("schema_version") == "0004_anchor_observation_hardening"
        and package.get("coding_memory_bench_bundled") is True
        and package.get("sqlite_vec_bundled") is True
        and package.get("restart_persistence") is True
        and package.get("first_health", {}).get("version") == "2.2.0"
    )
    result = "PASS" if branch == "main" and not dirty and package_passed else "FAIL"
    report = {
        "schema": "memoryos-v2.2-main-release-smoke@1",
        "generated_at": datetime.now(UTC).isoformat(),
        "result": result,
        "branch": branch,
        "commit": commit,
        "git_dirty_before_run": dirty,
        "executable_sha256": _sha256(executable),
        "package": package,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
