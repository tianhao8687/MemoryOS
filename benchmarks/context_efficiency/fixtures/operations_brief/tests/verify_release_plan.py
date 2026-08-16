from __future__ import annotations

import json
from pathlib import Path

EXPECTED = {
    "decision": "hold",
    "reason_code": "latency_slo_breach",
    "max_canary_percent": 5,
    "required_action": "rerun_latency_benchmark",
    "evidence_file": "evidence/current_metrics.md",
}


def main() -> None:
    value = json.loads(Path("decision/release_plan.json").read_text(encoding="utf-8"))
    if value != EXPECTED:
        raise SystemExit(f"release plan mismatch: {value!r}")
    print("release plan valid")


if __name__ == "__main__":
    main()
