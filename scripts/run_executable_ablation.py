from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from memoryos.evaluation.executable_ablation import (
    AblationArm,
    ExecutableAblationRun,
    ablation_run_from_report,
    analyze_executable_ablations,
    materialize_ablation_manifest,
    materialize_task_manifest,
)
from memoryos.evaluation.real_workload_models import ExperimentCondition
from memoryos.evaluation.real_workload_report import RunMode
from memoryos.evaluation.real_workload_runner import RealWorkloadRunner, load_runner_inputs
from memoryos.evaluation.real_workload_workspace import RepositoryWorkspaceManager
from memoryos.evaluation.retrieval_weight_calibration import (
    CalibrationPartition,
    observation_from_ablation_pair,
)
from memoryos.retrieval_v2.routing import load_routing_shadow_profile
from memoryos.retrieval_v2.rrf_shadow import load_rrf_channel_shadow_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Run a randomized, paired MemoryOS full/minus-memory executable ablation.")
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--memory-id", required=True)
    parser.add_argument(
        "--partition",
        required=True,
        choices=[partition.value for partition in CalibrationPartition],
    )
    parser.add_argument("--repeat-id")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--resume-full-report",
        type=Path,
        help="Reuse one completed, protocol-valid full-arm report after strict identity checks.",
    )
    parser.add_argument(
        "--resume-minus-report",
        type=Path,
        help="Reuse one completed, protocol-valid minus-arm report after strict identity checks.",
    )
    parser.add_argument("--order-seed", type=int, default=20260812)
    parser.add_argument("--rrf-channel-profile", type=Path)
    parser.add_argument("--routing-profile", type=Path)
    parser.add_argument("--embedding-base-url")
    parser.add_argument("--embedding-model")
    parser.add_argument(
        "--diagnostic-only",
        action="store_true",
        help="Run the pair without emitting a calibration training observation.",
    )
    parser.add_argument("--work-root", type=Path, default=Path("build/real-workload"))
    parser.add_argument(
        "--reuse-repository-cache-without-fetch",
        action="store_true",
        help=(
            "Use an existing origin-validated bare cache without refreshing its remote. "
            "The run still requires every pinned task and solution commit in that cache."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("build/ai-calibration/ablation-evidence"),
    )
    arguments = parser.parse_args()

    manifest, runtime = load_runner_inputs(arguments.manifest, arguments.runtime)
    channel_profile = (
        None
        if arguments.rrf_channel_profile is None
        else load_rrf_channel_shadow_profile(arguments.rrf_channel_profile)
    )
    routing_profile = (
        None
        if arguments.routing_profile is None
        else load_routing_shadow_profile(arguments.routing_profile)
    )
    if channel_profile is not None and routing_profile is not None:
        raise ValueError("executable ablation can use only one retrieval shadow profile")
    if (channel_profile is not None or routing_profile is not None) and not (
        arguments.diagnostic_only
    ):
        raise ValueError("retrieval shadows may run only with --diagnostic-only")
    if (arguments.embedding_base_url is None) != (arguments.embedding_model is None):
        raise ValueError("--embedding-base-url and --embedding-model must be set together")
    if channel_profile is not None and arguments.embedding_model is None:
        raise ValueError("public RRF shadow requires its real embedding provider")
    task_by_id = {task.id: task for task in manifest.tasks}
    try:
        task = task_by_id[arguments.task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task: {arguments.task_id}") from exc
    full_manifest = materialize_task_manifest(manifest, task_id=task.id)
    minus_manifest = materialize_ablation_manifest(
        manifest,
        task_id=task.id,
        excluded_memory_id=arguments.memory_id,
    )

    run_id = arguments.run_id or datetime.now(UTC).strftime("ablation-%Y%m%d-%H%M%S")
    repeat_id = arguments.repeat_id or run_id
    arm_specs = [
        (AblationArm.MEMORYOS_FULL, full_manifest, None),
        (AblationArm.MEMORYOS_MINUS_MEMORY, minus_manifest, arguments.memory_id),
    ]
    order_hash = hashlib.sha256(
        f"{arguments.order_seed}\x1f{repeat_id}\x1f{task.id}\x1f{arguments.memory_id}".encode()
    ).digest()
    if order_hash[0] & 1:
        arm_specs.reverse()

    output_root = arguments.output_root.resolve()
    aggregate_dir = output_root / run_id
    if aggregate_dir.exists():
        raise ValueError(f"refusing to reuse ablation output directory: {aggregate_dir}")
    workspace_manager = RepositoryWorkspaceManager(
        arguments.work_root / "repositories",
        refresh_existing_cache=not arguments.reuse_repository_cache_without_fetch,
    )
    runner = RealWorkloadRunner(arguments.work_root, workspace_manager=workspace_manager)
    expected_runtime_sha256 = _canonical_sha256(runtime.model_dump(mode="json"))
    resumed_reports = {
        AblationArm.MEMORYOS_FULL: arguments.resume_full_report,
        AblationArm.MEMORYOS_MINUS_MEMORY: arguments.resume_minus_report,
    }
    runs: list[ExecutableAblationRun] = []
    arm_order: list[str] = []
    resumed_arms: list[str] = []
    for arm, arm_manifest, excluded_memory_id in arm_specs:
        arm_order.append(arm.value)
        resume_path = resumed_reports[arm]
        if resume_path is None:
            arm_run_id = f"{run_id}-{_arm_suffix(arm)}"
            source_report = runner.run(
                arm_manifest,
                runtime,
                hidden_root=arguments.hidden_root,
                output_root=output_root,
                mode=RunMode.DRY_RUN,
                run_id=arm_run_id,
                conditions=[ExperimentCondition.MEMORYOS],
                order_seed=arguments.order_seed,
                rrf_channel_profile=channel_profile,
                routing_profile=routing_profile,
                embedding_base_url=arguments.embedding_base_url,
                embedding_model=arguments.embedding_model,
            )
        else:
            source_report = _load_json_object(resume_path)
            if (
                channel_profile is not None
                or routing_profile is not None
                or arguments.embedding_model is not None
            ):
                _validate_resumed_provider(
                    source_report,
                    shadow_profile_sha256=(
                        channel_profile.digest() if channel_profile is not None else None
                    ),
                    routing_profile_sha256=(
                        routing_profile.digest() if routing_profile is not None else None
                    ),
                    embedding_model=arguments.embedding_model,
                )
            resumed_arms.append(arm.value)
        converted = ablation_run_from_report(
            source_report,
            task,
            arm=arm,
            repeat_id=repeat_id,
            excluded_memory_id=excluded_memory_id,
            expected_manifest_digest=arm_manifest.digest(),
            expected_runtime_sha256=expected_runtime_sha256,
            registered_memories=arm_manifest.memories,
        )
        if not converted.protocol_valid:
            source = "resumed" if resume_path is not None else "fresh"
            raise ValueError(f"{source} {arm.value} arm failed a protocol validity gate")
        runs.append(converted)

    ablation_report = analyze_executable_ablations(runs)
    full_run = next(run for run in runs if run.arm is AblationArm.MEMORYOS_FULL)
    minus_run = next(run for run in runs if run.arm is AblationArm.MEMORYOS_MINUS_MEMORY)
    observation = (
        None
        if arguments.diagnostic_only
        else observation_from_ablation_pair(
            full_run,
            minus_run,
            observation_id="ablation-"
            + hashlib.sha256(
                f"{run_id}\x1f{repeat_id}\x1f{task.id}\x1f{arguments.memory_id}".encode()
            ).hexdigest()[:32],
            partition=CalibrationPartition(arguments.partition),
        )
    )
    aggregate_dir.mkdir(parents=True)
    _write_jsonl(aggregate_dir / "ablation-runs.jsonl", runs)
    _write_jsonl(
        aggregate_dir / "training-observations.jsonl",
        [] if observation is None else [observation],
    )
    _write_json(
        aggregate_dir / "ablation-report.json",
        ablation_report.model_dump(mode="json"),
    )
    summary = {
        "status": ablation_report.status,
        "run_id": run_id,
        "repeat_id": repeat_id,
        "arm_order": arm_order,
        "resumed_arms": resumed_arms,
        "evidence_type": runtime.evidence_type.value,
        "protocol_valid_runs": sum(run.protocol_valid for run in runs),
        "effect_status": ablation_report.effects[0].status.value,
        "informative_pairs": ablation_report.effects[0].informative_pairs,
        "training_observations": int(observation is not None),
        "diagnostic_only": arguments.diagnostic_only,
        "shadow_profile_sha256": (None if channel_profile is None else channel_profile.digest()),
        "embedding_model": arguments.embedding_model,
        "production_eligible": ablation_report.production_eligible,
        "output": str(aggregate_dir),
    }
    _write_json(aggregate_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not all(run.protocol_valid for run in runs):
        raise SystemExit(1)


def _arm_suffix(arm: AblationArm) -> str:
    return "full" if arm is AblationArm.MEMORYOS_FULL else "minus"


def _write_jsonl(path: Path, values: Sequence[BaseModel]) -> None:
    payload = "".join(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for value in values
    )
    path.write_text(payload, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid resumed arm report: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"resumed arm report must be a JSON object: {path}")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_resumed_provider(
    report: dict[str, Any],
    *,
    shadow_profile_sha256: str | None,
    routing_profile_sha256: str | None,
    embedding_model: str | None,
) -> None:
    if report.get("scoring_profile_sha256") != shadow_profile_sha256:
        raise ValueError("resumed arm shadow profile does not match this run")
    if report.get("routing_profile_sha256") != routing_profile_sha256:
        raise ValueError("resumed arm routing profile does not match this run")
    provider = report.get("embedding_provider")
    if not isinstance(provider, dict) or provider.get("model") != embedding_model:
        raise ValueError("resumed arm embedding provider does not match this run")


if __name__ == "__main__":
    main()
