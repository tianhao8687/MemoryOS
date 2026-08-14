from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _report_path(*, frozen_name: str, source_parts: tuple[str, ...], label: str) -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    candidates = []
    if frozen_root:
        candidates.append(Path(str(frozen_root)) / "verification" / frozen_name)
    candidates.append(Path(__file__).resolve().parents[2].joinpath(*source_parts))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{label} report has not been generated")


def memorybench_report_path() -> Path:
    return _report_path(
        frozen_name="memorybench-report.json",
        source_parts=("docs", "verification", "v2", "memorybench-report.json"),
        label="MemoryBench V2",
    )


def coding_memory_bench_report_path() -> Path:
    return _report_path(
        frozen_name="coding-memory-bench.json",
        source_parts=("docs", "verification", "v2.1", "coding-memory-bench.json"),
        label="CodingMemoryBench fixture regression",
    )


def load_memorybench_report() -> dict[str, Any]:
    value = json.loads(memorybench_report_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "memorybench-v2-report@1":
        raise ValueError("MemoryBench V2 report schema is invalid")
    return value


def load_coding_memory_bench_report() -> dict[str, Any]:
    value = json.loads(coding_memory_bench_report_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "coding-memory-bench-v2.1@1":
        raise ValueError("CodingMemoryBench fixture regression report schema is invalid")
    return value


__all__ = [
    "coding_memory_bench_report_path",
    "load_coding_memory_bench_report",
    "load_memorybench_report",
    "memorybench_report_path",
]
