from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from memoryos.evaluation.context_efficiency import (
    MEMORYOS_CONTEXT_CONDITIONS,
    ContextEfficiencyCondition,
)
from memoryos.evaluation.context_efficiency_runner import (
    ContextEfficiencyRunConfig,
    ContextEfficiencyRunner,
    load_context_efficiency_inputs,
)
from memoryos.evaluation.provider_usage import CachePhase


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the executable MemoryOS V2.3 context-efficiency experiment."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path("build/context-efficiency-work-v23"))
    parser.add_argument(
        "--condition-work-root",
        action="append",
        default=[],
        metavar="CONDITION=PATH",
        help="Route one condition's disposable workspaces to a separate physical root.",
    )
    parser.add_argument(
        "--docker-bind-root-map",
        action="append",
        default=[],
        metavar="CONTAINER_ROOT=DOCKER_HOST_ROOT",
        help=(
            "Translate an outer-container workspace prefix to the equivalent path "
            "visible to the Docker daemon used by hidden tests."
        ),
    )
    parser.add_argument(
        "--usage-guard-file",
        action="append",
        default=[],
        metavar="CONDITION=PATH",
        help=(
            "Check a controller-owned JSON stop file synchronously before each provider "
            "dispatch for one condition."
        ),
    )
    parser.add_argument("--hidden-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--tasks", type=int)
    parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="Run one exact manifest task id; repeat to select and order multiple tasks.",
    )
    parser.add_argument("--order-seed", type=int, default=20260815)
    parser.add_argument("--budget-tokens", type=int, default=6000)
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=[condition.value for condition in ContextEfficiencyCondition],
        default=[condition.value for condition in MEMORYOS_CONTEXT_CONDITIONS],
    )
    parser.add_argument(
        "--cache-phases",
        nargs="+",
        choices=[phase.value for phase in CachePhase],
        default=[CachePhase.COLD.value],
    )
    arguments = parser.parse_args()

    manifest_path = arguments.manifest.resolve()
    manifest, runtime = load_context_efficiency_inputs(manifest_path, arguments.runtime.resolve())
    config = ContextEfficiencyRunConfig(
        conditions=tuple(ContextEfficiencyCondition(value) for value in arguments.conditions),
        cache_phases=tuple(CachePhase(value) for value in arguments.cache_phases),
        order_seed=arguments.order_seed,
        budget_tokens=arguments.budget_tokens,
    )
    run_id = arguments.run_id or datetime.now(UTC).strftime("context-%Y%m%d-%H%M%S")
    hidden_root = (
        arguments.hidden_root.resolve()
        if arguments.hidden_root is not None
        else manifest_path.parent / "hidden"
    )
    condition_work_roots: dict[ContextEfficiencyCondition, Path] = {}
    for item in arguments.condition_work_root:
        condition_text, separator, path_text = item.partition("=")
        if not separator or not path_text:
            parser.error("--condition-work-root must use CONDITION=PATH")
        try:
            condition = ContextEfficiencyCondition(condition_text)
        except ValueError:
            parser.error(f"unknown condition in --condition-work-root: {condition_text}")
        if condition in condition_work_roots:
            parser.error(f"duplicate --condition-work-root for {condition.value}")
        condition_work_roots[condition] = Path(path_text)
    docker_bind_root_maps: dict[Path, Path] = {}
    for item in arguments.docker_bind_root_map:
        source_text, separator, target_text = item.partition("=")
        if not separator or not source_text or not target_text:
            parser.error("--docker-bind-root-map must use CONTAINER_ROOT=DOCKER_HOST_ROOT")
        source = Path(source_text)
        if source in docker_bind_root_maps:
            parser.error(f"duplicate --docker-bind-root-map source: {source_text}")
        docker_bind_root_maps[source] = Path(target_text)
    condition_usage_guard_files: dict[ContextEfficiencyCondition, Path] = {}
    for item in arguments.usage_guard_file:
        condition_text, separator, path_text = item.partition("=")
        if not separator or not path_text:
            parser.error("--usage-guard-file must use CONDITION=PATH")
        try:
            condition = ContextEfficiencyCondition(condition_text)
        except ValueError:
            parser.error(f"unknown condition in --usage-guard-file: {condition_text}")
        if condition in condition_usage_guard_files:
            parser.error(f"duplicate --usage-guard-file for {condition.value}")
        condition_usage_guard_files[condition] = Path(path_text)
    if arguments.tasks is not None and arguments.task_id is not None:
        parser.error("--tasks and --task-id are mutually exclusive")
    summary = ContextEfficiencyRunner(
        arguments.work_root,
        condition_work_roots=condition_work_roots,
        condition_usage_guard_files=condition_usage_guard_files,
        docker_bind_root_maps=docker_bind_root_maps,
    ).run(
        manifest,
        runtime,
        hidden_root=hidden_root,
        output_root=arguments.output,
        run_id=run_id,
        config=config,
        task_limit=arguments.tasks,
        task_ids=tuple(arguments.task_id) if arguments.task_id is not None else None,
    )
    print(
        f"{summary['status']}: {summary['run_count']} runs; "
        f"external_blockers={summary['external_blocker_count']}"
    )
    if summary["status"] == "external_blocker":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
