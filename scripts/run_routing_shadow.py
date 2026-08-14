from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from memoryos.evaluation.executable_ablation import materialize_task_manifest
from memoryos.evaluation.real_workload_models import ExperimentCondition
from memoryos.evaluation.real_workload_report import RunMode
from memoryos.evaluation.real_workload_runner import RealWorkloadRunner, load_runner_inputs
from memoryos.evaluation.retrieval_routing_evaluation import routing_evaluation_from_reports
from memoryos.retrieval_v2.routing import load_routing_shadow_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a randomized paired production-baseline/routing-shadow task."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--repeat-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--order-seed", type=int, default=20260813)
    parser.add_argument("--work-root", type=Path, default=Path("build/real-workload"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("build/ai-calibration/routing-shadow-evidence"),
    )
    arguments = parser.parse_args()

    manifest, runtime = load_runner_inputs(arguments.manifest, arguments.runtime)
    profile = load_routing_shadow_profile(arguments.profile)
    tasks = {task.id: task for task in manifest.tasks}
    try:
        task = tasks[arguments.task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task: {arguments.task_id}") from exc
    task_manifest = materialize_task_manifest(manifest, task_id=task.id)

    run_id = arguments.run_id or datetime.now(UTC).strftime("routing-shadow-%Y%m%d-%H%M%S")
    output_root = arguments.output_root.resolve()
    aggregate_dir = output_root / run_id
    if aggregate_dir.exists():
        raise ValueError(f"refusing to reuse routing-shadow output directory: {aggregate_dir}")
    arms = [("baseline", None), ("candidate", profile)]
    order_hash = hashlib.sha256(
        f"{arguments.order_seed}\x1f{arguments.repeat_id}\x1f{task.id}\x1f{profile.digest()}".encode()
    ).digest()
    if order_hash[0] & 1:
        arms.reverse()

    reports: dict[str, dict[str, object]] = {}
    runner = RealWorkloadRunner(arguments.work_root)
    for arm_name, routing_profile in arms:
        reports[arm_name] = runner.run(
            task_manifest,
            runtime,
            hidden_root=arguments.hidden_root,
            output_root=output_root,
            mode=RunMode.DRY_RUN,
            run_id=f"{run_id}-{arm_name}",
            conditions=[ExperimentCondition.MEMORYOS],
            order_seed=arguments.order_seed,
            routing_profile=routing_profile,
        )

    evaluation = routing_evaluation_from_reports(
        profile,
        reports["baseline"],
        reports["candidate"],
        task,
        repeat_id=arguments.repeat_id,
    )
    aggregate_dir.mkdir(parents=True)
    _write_json(
        aggregate_dir / "retrieval-routing-shadow-profile.json",
        profile.model_dump(mode="json"),
    )
    _write_jsonl(
        aggregate_dir / "routing-evaluations.jsonl",
        [evaluation.model_dump(mode="json")],
    )
    summary = {
        "status": "routing_shadow_complete",
        "run_id": run_id,
        "repeat_id": arguments.repeat_id,
        "arm_order": [name for name, _ in arms],
        "routing_profile_sha256": profile.digest(),
        "recipe_registry_sha256": profile.recipe_registry_sha256,
        "protocol_valid": evaluation.protocol_valid,
        "baseline_success": evaluation.baseline_success,
        "candidate_success": evaluation.candidate_success,
        "recommended_recipe_counts": evaluation.recommended_recipe_counts,
        "executed_recipe_counts": evaluation.executed_recipe_counts,
        "production_eligible": False,
        "output": str(aggregate_dir),
    }
    _write_json(aggregate_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not evaluation.protocol_valid:
        raise SystemExit(1)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, payloads: list[object]) -> None:
    path.write_text(
        "".join(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for payload in payloads
        ),
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
