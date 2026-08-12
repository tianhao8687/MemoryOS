from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from memoryos.evaluation.ai_calibration_protocol import load_ai_calibration_protocol
from memoryos.evaluation.executable_ablation import materialize_task_manifest
from memoryos.evaluation.real_workload_models import DatasetTier, ExperimentCondition
from memoryos.evaluation.real_workload_report import RunMode
from memoryos.evaluation.real_workload_runner import RealWorkloadRunner, load_runner_inputs
from memoryos.evaluation.retrieval_weight_calibration import (
    LearnedWeightProfile,
    shadow_scoring_profile_from_learned,
    weight_evaluation_from_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a randomized paired frozen-baseline/candidate retrieval shadow task."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("benchmarks/ai_calibration_v1/protocol.json"),
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--repeat-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--order-seed", type=int, default=20260812)
    parser.add_argument("--work-root", type=Path, default=Path("build/real-workload"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("build/ai-calibration/weight-shadow-evidence"),
    )
    arguments = parser.parse_args()

    manifest, runtime = load_runner_inputs(arguments.manifest, arguments.runtime)
    protocol = load_ai_calibration_protocol(arguments.protocol)
    profile = _load_profile(arguments.profile)
    if profile.protocol_sha256 != protocol.promotion.expected_training_protocol_sha256:
        raise ValueError("candidate profile does not use the frozen AI calibration protocol")
    shadow_profile = shadow_scoring_profile_from_learned(profile)
    tasks = {task.id: task for task in manifest.tasks}
    try:
        task = tasks[arguments.task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task: {arguments.task_id}") from exc
    task_manifest = materialize_task_manifest(manifest, task_id=task.id)
    seen_repositories = set(profile.training_repositories) | set(profile.development_repositories)
    sealed = (
        manifest.tier is DatasetTier.PUBLIC_REPLAY
        and task.repository_id not in seen_repositories
        and task.solution_commit is not None
        and task.source_published_at is not None
        and task.hidden_test.hidden_patch_sha256 is not None
    )

    run_id = arguments.run_id or datetime.now(UTC).strftime("weight-shadow-%Y%m%d-%H%M%S")
    output_root = arguments.output_root.resolve()
    aggregate_dir = output_root / run_id
    if aggregate_dir.exists():
        raise ValueError(f"refusing to reuse weight-shadow output directory: {aggregate_dir}")
    arms = [("baseline", None), ("candidate", shadow_profile)]
    order_hash = hashlib.sha256(
        f"{arguments.order_seed}\x1f{arguments.repeat_id}\x1f{task.id}\x1f{profile.profile_sha256}".encode()
    ).digest()
    if order_hash[0] & 1:
        arms.reverse()

    reports: dict[str, dict[str, object]] = {}
    runner = RealWorkloadRunner(arguments.work_root)
    for arm_name, scoring_profile in arms:
        reports[arm_name] = runner.run(
            task_manifest,
            runtime,
            hidden_root=arguments.hidden_root,
            output_root=output_root,
            mode=RunMode.DRY_RUN,
            run_id=f"{run_id}-{arm_name}",
            conditions=[ExperimentCondition.MEMORYOS],
            order_seed=arguments.order_seed,
            scoring_profile=scoring_profile,
        )

    evaluation = weight_evaluation_from_reports(
        profile,
        reports["baseline"],
        reports["candidate"],
        task,
        repeat_id=arguments.repeat_id,
        sealed=sealed,
    )
    aggregate_dir.mkdir(parents=True)
    _write_json(
        aggregate_dir / "shadow-retrieval-profile.json",
        shadow_profile.model_dump(mode="json"),
    )
    _write_jsonl(
        aggregate_dir / "weight-evaluations.jsonl",
        [evaluation.model_dump(mode="json")],
    )
    summary = {
        "status": "weight_shadow_complete",
        "run_id": run_id,
        "repeat_id": arguments.repeat_id,
        "arm_order": [name for name, _ in arms],
        "candidate_profile_sha256": profile.profile_sha256,
        "candidate_scoring_profile_sha256": shadow_profile.digest(),
        "sealed": evaluation.sealed,
        "protocol_valid": evaluation.protocol_valid,
        "baseline_success": evaluation.baseline_success,
        "candidate_success": evaluation.candidate_success,
        "production_eligible": False,
        "output": str(aggregate_dir),
    }
    _write_json(aggregate_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not evaluation.protocol_valid:
        raise SystemExit(1)


def _load_profile(path: Path) -> LearnedWeightProfile:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid learned weight profile: {path}") from exc
    return TypeAdapter(LearnedWeightProfile).validate_python(payload)


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
