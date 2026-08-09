from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from memoryos.evaluation.real_agent import (
    AgentEndpoint,
    RealPairedAgentRunner,
    external_blocker_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the V2.1 paired real coding-agent protocol")
    parser.add_argument("--tasks", type=int, default=50)
    parser.add_argument("--output", type=Path, default=Path("docs/verification/v2.1/agent-ab.json"))
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    if args.tasks < 50:
        parser.error("--tasks must be at least 50")
    base_url = os.environ.get("MEMORYOS_AGENT_BASE_URL")
    model = os.environ.get("MEMORYOS_AGENT_MODEL")
    if not base_url or not model:
        report = external_blocker_report(
            tasks=args.tasks,
            reason=(
                "MEMORYOS_AGENT_BASE_URL and MEMORYOS_AGENT_MODEL are not configured; no external "
                "model endpoint or credentials were supplied in this environment."
            ),
        )
    else:
        runner = RealPairedAgentRunner(
            AgentEndpoint(
                base_url=base_url,
                model=model,
                api_key=os.environ.get("MEMORYOS_AGENT_API_KEY"),
            )
        )
        report = runner.run(tasks=args.tasks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Paired agent evidence: {args.output}")
    print(f"Status: {report['status']}; effect claim: {report['effect_claim']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
