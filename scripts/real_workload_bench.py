from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from memoryos.evaluation.real_workload_report import RunMode
from memoryos.evaluation.real_workload_runner import RealWorkloadRunner, load_runner_inputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the isolated three-condition MemoryOS real-workload benchmark."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path("build/real-workload"))
    parser.add_argument("--output-root", type=Path, default=Path("docs/verification/v2.2"))
    parser.add_argument("--mode", choices=[mode.value for mode in RunMode], default="dry_run")
    parser.add_argument("--tasks", type=int)
    parser.add_argument("--order-seed", type=int, default=20260810)
    parser.add_argument("--run-id")
    arguments = parser.parse_args()
    manifest, runtime = load_runner_inputs(arguments.manifest, arguments.runtime)
    run_id = arguments.run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    report = RealWorkloadRunner(arguments.work_root).run(
        manifest,
        runtime,
        hidden_root=arguments.hidden_root,
        output_root=arguments.output_root,
        mode=RunMode(arguments.mode),
        run_id=run_id,
        task_limit=arguments.tasks,
        order_seed=arguments.order_seed,
    )
    print(
        f"{report['status']}: {report['sample_size']} tasks; effect_claim={report['effect_claim']}"
    )
    if not report["protocol_valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
