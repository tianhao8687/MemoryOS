from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.relative_usage_guard import RelativeUsageGuardController


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enforce the frozen relative-use ceiling across three DeepSeek arms."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    arguments = parser.parse_args()

    config = json.loads(arguments.config.read_text(encoding="utf-8"))
    controller = RelativeUsageGuardController.from_config(config)
    controller.initialize()
    print("relative_usage_guard_ready", flush=True)
    report = controller.run(
        poll_seconds=arguments.poll_seconds,
        timeout_seconds=arguments.timeout_seconds,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for condition, decision in report["decisions"].items():
        print(
            f"relative_usage_guard_stop condition={condition} "
            f"metric={decision['metric']} observed={decision['observed']} "
            f"ceiling={decision['ceiling']}",
            flush=True,
        )
    print(f"relative_usage_guard_complete status={report['status']}", flush=True)
    if report["status"] != "completed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
