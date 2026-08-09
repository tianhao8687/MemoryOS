from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def memorybench_report_path() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if frozen_root:
        candidates.append(Path(str(frozen_root)) / "verification" / "memorybench-report.json")
    candidates.append(
        Path(__file__).resolve().parents[2]
        / "docs"
        / "verification"
        / "v2"
        / "memorybench-report.json"
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("MemoryBench V2 report has not been generated")


def load_memorybench_report() -> dict[str, Any]:
    value = json.loads(memorybench_report_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "memorybench-v2-report@1":
        raise ValueError("MemoryBench V2 report schema is invalid")
    return value


__all__ = ["load_memorybench_report", "memorybench_report_path"]
