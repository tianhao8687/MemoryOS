from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from memoryos.evaluation.context_efficiency import ContextEfficiencyCondition
from memoryos.evaluation.context_efficiency_runner import load_context_efficiency_inputs
from memoryos.evaluation.context_efficiency_runtime import (
    ConditionPolicy,
    MemoryOSToolBackend,
)
from memoryos.evaluation.deepseek_harness_agent import (
    DeepSeekHarnessCodingAgent,
    DeepSeekHarnessRuntime,
)
from memoryos.evaluation.openai_compatible_coding_agent import AgentRunStatus, ToolEvent
from memoryos.evaluation.provider_usage import (
    CachePhase,
    ProviderUsageRecord,
    aggregate_usage,
)
from memoryos.evaluation.real_workload_models import ExperimentCondition
from memoryos.evaluation.real_workload_scoring import HiddenTestRunner
from memoryos.evaluation.real_workload_workspace import (
    MaterializedWorkspace,
    PreparedRepository,
    RepositoryWorkspaceManager,
    _git_control_plane_digest,
)


def _json_default(value: Any) -> str:
    if isinstance(value, (Decimal, Path)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)
            + "\n"
        )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for suffix in range(1, 10_000):
        candidate = path.with_name(f"{path.name}-recovery-{suffix:03d}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate a recovery path beside {path}")


def _available_run_id(manager: RepositoryWorkspaceManager, run_id: str) -> str:
    if not (manager.runs_root / run_id).exists():
        return run_id
    for suffix in range(1, 10_000):
        candidate = f"{run_id}-recovery-{suffix:03d}"
        if not (manager.runs_root / candidate).exists():
            return candidate
    raise RuntimeError(f"could not allocate a recovery scoring run id for {run_id}")


def _usage_key(item: ProviderUsageRecord) -> tuple[str, str, int, str]:
    return item.run_id, item.condition, item.step_index, item.request_sha256


def _event_key(item: ToolEvent) -> tuple[str, str, int, str]:
    return item.run_id, item.condition, item.event_index, item.arguments_sha256


def _remove_stale_git_index_lock(workspace: Path, condition: str) -> None:
    stale_index_lock = workspace / ".git" / "index.lock"
    if not stale_index_lock.exists():
        return
    if not stale_index_lock.is_file() or stale_index_lock.is_symlink():
        raise RuntimeError(f"refusing to remove an unsafe Git index lock: {stale_index_lock}")
    stale_index_lock.unlink()
    print(f"recovered_stale_git_lock condition={condition}", flush=True)


def _bounded_runtime(runtime: DeepSeekHarnessRuntime, remaining: int) -> DeepSeekHarnessRuntime:
    no_patch = min(runtime.no_patch_request_limit, remaining)
    patch = min(runtime.patch_preserving_request_limit, remaining)
    payload = runtime.model_dump(mode="json")
    payload.update(
        {
            "no_patch_request_limit": no_patch,
            "patch_preserving_request_limit": max(no_patch, patch),
            "hard_request_limit": remaining,
        }
    )
    return DeepSeekHarnessRuntime.model_validate(payload)


def _existing_workspace(
    root: Path,
    relative: str,
    *,
    repository_id: str,
    task_id: str,
    condition: ContextEfficiencyCondition,
    base_commit: str,
) -> MaterializedWorkspace:
    path = (root / relative).resolve(strict=True)
    return MaterializedWorkspace(
        repository_id=repository_id,
        task_id=task_id,
        condition=cast(ExperimentCondition, condition),
        path=path,
        base_commit=base_commit,
        git_control_sha256=_git_control_plane_digest(path),
    )


def _condition_summary(
    condition: str,
    *,
    initial_records: dict[str, dict[str, Any]],
    initial_usage: list[ProviderUsageRecord],
    initial_events: list[ToolEvent],
    rounds: list[dict[str, Any]],
    continuation_usage: list[ProviderUsageRecord],
    continuation_events: list[ToolEvent],
) -> dict[str, Any]:
    initial = initial_records[condition]
    own_rounds = [item for item in rounds if item["condition"] == condition]
    own_initial_usage = [item for item in initial_usage if item.condition == condition]
    own_continuation_usage = [item for item in continuation_usage if item.condition == condition]
    total_usage = [*own_initial_usage, *own_continuation_usage]
    totals = aggregate_usage(total_usage)
    own_initial_events = [item for item in initial_events if item.condition == condition]
    own_continuation_events = [item for item in continuation_events if item.condition == condition]
    final_round = own_rounds[-1] if own_rounds else None
    return {
        "hidden_test_success": bool(final_round and final_round["hidden_test_success"]),
        "rounds_to_success": len(own_rounds),
        "initial_provider_attempts": initial["provider_attempts"],
        "continuation_provider_attempts": sum(item["provider_attempts"] for item in own_rounds),
        "total_provider_attempts": initial["provider_attempts"]
        + sum(item["provider_attempts"] for item in own_rounds),
        "initial_completed_requests": len(own_initial_usage),
        "continuation_completed_requests": len(own_continuation_usage),
        "total_completed_requests": totals.requests,
        "total_input_tokens": totals.input_tokens,
        "total_cache_hit_tokens": totals.cache_hit_tokens,
        "total_cache_miss_tokens": totals.cache_miss_tokens,
        "total_output_tokens": totals.output_tokens,
        "total_cost_usd": str(totals.cost_usd) if totals.cost_usd is not None else None,
        "initial_latency_seconds": initial["record"]["latency_seconds"],
        "continuation_latency_seconds": round(
            sum(item["latency_seconds"] for item in own_rounds), 6
        ),
        "total_latency_seconds": round(
            initial["record"]["latency_seconds"]
            + sum(item["latency_seconds"] for item in own_rounds),
            6,
        ),
        "initial_memory_tool_calls": len(own_initial_events),
        "continuation_memory_tool_calls": len(own_continuation_events),
        "total_memory_tool_calls": len(own_initial_events) + len(own_continuation_events),
        "final_patch_sha256": final_round["patch_sha256"] if final_round else None,
        "final_patch_bytes": final_round["patch_bytes"] if final_round else 0,
        "final_changed_files": final_round["changed_files"] if final_round else [],
        "session_id": final_round["session_id"] if final_round else None,
    }


def _render_report(report: dict[str, Any]) -> str:
    labels = {
        "no_memory": "A 无记忆",
        "msc_full": "B 完整记忆",
        "msc_progressive": "C 渐进记忆",
    }
    lines = [
        "# DeepSeek V4 Flash 原会话续跑 A/B/C 结果",
        "",
        "三组均从第一轮各自的同一个 Harness session 和工作区继续; 成功条件是同一冻结隐藏测试通过。",
        "",
        "| 组别 | 成功 | 续跑轮次 | 总尝试/完整响应 | 总输入 token | "
        "总输出 token | 总费用 | 总耗时 | 最终补丁 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("no_memory", "msc_full", "msc_progressive"):
        if condition not in report["conditions"]:
            continue
        item = report["conditions"][condition]
        lines.append(
            "| {label} | {success} | {rounds} | {attempts}/{requests} | {input_tokens:,} | "
            "{output_tokens:,} | ${cost} | {latency:.2f}s | {patch} bytes |".format(
                label=labels[condition],
                success="通过" if item["hidden_test_success"] else "未通过",
                rounds=item["rounds_to_success"],
                attempts=item["total_provider_attempts"],
                requests=item["total_completed_requests"],
                input_tokens=item["total_input_tokens"],
                output_tokens=item["total_output_tokens"],
                cost=item["total_cost_usd"] or "未知",
                latency=item["total_latency_seconds"],
                patch=item["final_patch_bytes"],
            )
        )
    lines.extend(
        [
            "",
            "注: 尝试数包含供应商调用前钩子记录的尝试; "
            "完整响应才有精确 token 和费用。总数包含第一轮与本次续跑。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume the frozen DeepSeek medium-task A/B/C sessions until acceptance."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hidden-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument(
        "--condition",
        choices=[item.value for item in ContextEfficiencyCondition],
        help="Run one isolated condition; launch three processes for parallel A/B/C.",
    )
    parser.add_argument(
        "--single-natural-run",
        action="store_true",
        help=(
            "Send one continuation message, disable request/token budget probing, "
            "and never automatically send another continuation message."
        ),
    )
    arguments = parser.parse_args()

    manifest, loaded_runtime = load_context_efficiency_inputs(
        arguments.manifest.resolve(), arguments.runtime.resolve()
    )
    if not isinstance(loaded_runtime, DeepSeekHarnessRuntime):
        raise TypeError("continuation requires a DeepSeek Harness runtime")
    lock = json.loads(arguments.lock.read_text(encoding="utf-8"))
    selected_conditions = (
        [arguments.condition] if arguments.condition is not None else lock["frozen_order"]
    )
    unknown_conditions = set(selected_conditions) - set(lock["frozen_order"])
    if unknown_conditions:
        raise ValueError(f"condition is not present in the continuation lock: {unknown_conditions}")
    campaign_root = arguments.campaign_root.resolve(strict=True)
    output = arguments.output.resolve()
    resuming_output = output.exists()
    if resuming_output and (output / "report.json").exists():
        raise ValueError(f"refusing to resume a completed continuation output: {output}")
    output.mkdir(parents=True, exist_ok=resuming_output)
    for name in ("patches", "hidden-tests", "results"):
        (output / name).mkdir(exist_ok=resuming_output)

    task = next(item for item in manifest.tasks if item.id == lock["task_id"])
    repository = next(item for item in manifest.repositories if item.id == task.repository_id)
    seeds_by_id = {item.id: item for item in manifest.memories}
    selected_seeds = [seeds_by_id[item] for item in task.memory_seed_ids]
    initial_records_list = _read_jsonl(campaign_root / "output" / "records.jsonl")
    initial_records = {item["record"]["condition"]: item for item in initial_records_list}
    initial_usage = [
        ProviderUsageRecord.model_validate(item)
        for item in _read_jsonl(campaign_root / "output" / "provider-usage.jsonl")
    ]
    initial_events = [
        ToolEvent.model_validate(item)
        for item in _read_jsonl(campaign_root / "output" / "tool-events.jsonl")
    ]
    rounds_path = output / "rounds.jsonl"
    usage_path = output / "provider-usage.jsonl"
    events_path = output / "tool-events.jsonl"
    for path in (rounds_path, usage_path, events_path):
        path.touch(exist_ok=resuming_output)
    rounds = _read_jsonl(rounds_path)
    continuation_usage = [
        ProviderUsageRecord.model_validate(item) for item in _read_jsonl(usage_path)
    ]
    continuation_events = [ToolEvent.model_validate(item) for item in _read_jsonl(events_path)]
    solved = {item["condition"] for item in rounds if item.get("hidden_test_success") is True}
    round_index = 0

    attempt_ceiling = (
        "disabled"
        if arguments.single_natural_run
        else lock["overall_attempt_safety_ceiling_per_condition_including_initial"]
    )
    print(
        f"continuation_start conditions={','.join(selected_conditions)} "
        f"attempt_ceiling={attempt_ceiling}",
        flush=True,
    )
    while len(solved & set(selected_conditions)) != len(selected_conditions):
        round_index += 1
        for condition_text in selected_conditions:
            if condition_text in solved:
                continue
            existing_round = next(
                (
                    item
                    for item in rounds
                    if item["round"] == round_index and item["condition"] == condition_text
                ),
                None,
            )
            if existing_round is not None:
                if existing_round["hidden_test_success"]:
                    solved.add(condition_text)
                continue
            condition = ContextEfficiencyCondition(condition_text)
            condition_lock = lock["conditions"][condition_text]
            initial_attempts = int(initial_records[condition_text]["provider_attempts"])
            used_attempts = sum(
                item["provider_attempts"] for item in rounds if item["condition"] == condition_text
            )
            if arguments.single_natural_run:
                remaining = -1
                runtime_payload = loaded_runtime.model_dump(mode="json")
                runtime_payload["run_timeout_seconds"] = 7200
                runtime = DeepSeekHarnessRuntime.model_validate(runtime_payload)
            else:
                remaining = (
                    int(lock["overall_attempt_safety_ceiling_per_condition_including_initial"])
                    - initial_attempts
                    - used_attempts
                )
                if remaining <= 0:
                    raise RuntimeError(
                        f"{condition_text} reached the frozen emergency attempt ceiling "
                        "before passing"
                    )
                runtime = _bounded_runtime(loaded_runtime, remaining)
            root = Path(condition_lock["filesystem_root"]).resolve(strict=True)
            workspace = _existing_workspace(
                root,
                condition_lock["workspace_relative"],
                repository_id=task.repository_id,
                task_id=task.id,
                condition=condition,
                base_commit=task.base_commit,
            )
            manager = RepositoryWorkspaceManager(
                root / "repositories",
                refresh_existing_cache=False,
                include_condition_in_workspace_path=False,
            )
            prepared = PreparedRepository(
                repository_id=repository.id,
                mirror_path=(
                    campaign_root / "work" / "repositories" / "cache" / f"{repository.id}.git"
                ).resolve(strict=True),
                source_fingerprint=hashlib.sha256(repository.clone_url.encode()).hexdigest(),
            )
            manager.assert_manifest_commits(prepared, [task])
            policy = ConditionPolicy.for_condition(condition)
            prompt = (
                lock["initial_prompt"]
                if not any(item["condition"] == condition_text for item in rounds)
                else lock["failed_acceptance_prompt"]
            )
            run_id = f"{lock['continuation_run_prefix']}-r{round_index:03d}"
            state_dir = (
                root
                / ".memoryos-harness"
                / lock["original_run_id"]
                / "continuations"
                / f"round-{round_index:03d}"
                / task.id
            )
            recovered = state_dir.exists()
            if recovered:
                if not (state_dir / "run-stderr.log").is_file():
                    raise RuntimeError(
                        f"cannot recover incomplete Harness state directory: {state_dir}"
                    )
                _remove_stale_git_index_lock(workspace.path, condition_text)
                result_usage = [
                    ProviderUsageRecord.model_validate_json(line)
                    for line in (state_dir / "provider-usage.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line
                ]
                result_events = [
                    item
                    for item in continuation_events
                    if item.run_id == run_id and item.condition == condition_text
                ]
                provider_attempts = len(
                    [
                        line
                        for line in (state_dir / "provider-attempts.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line
                    ]
                )
                message = (state_dir / "run-stderr.log").read_text(
                    encoding="utf-8", errors="replace"
                )[-4000:]
                agent_status = AgentRunStatus.FAILED.value
                agent_failure_reason = (
                    "harness_budget_exhausted" if "budget" in message.lower() else "harness_failed"
                )
                latency_key = f"{round_index}:{condition_text}"
                latency = float(
                    lock.get("recovery_latency_seconds", {}).get(
                        latency_key, sum(item.latency_seconds for item in result_usage)
                    )
                )
                print(
                    f"condition_recover round={round_index} condition={condition_text} "
                    f"attempts={provider_attempts} responses={len(result_usage)}",
                    flush=True,
                )
            else:
                memory: MemoryOSToolBackend | None = None
                if policy.memory_enabled:
                    memory = MemoryOSToolBackend(
                        data_dir=(campaign_root / condition_lock["memory_relative_to_campaign"]),
                        policy=policy,
                        task=task.prompt,
                        repository=task.repository_id,
                        seeds=selected_seeds,
                        seed_database=False,
                        budget_tokens=1200,
                    )
                remaining_label = "unlimited" if arguments.single_natural_run else remaining
                print(
                    f"condition_start round={round_index} condition={condition_text} "
                    f"remaining_attempts={remaining_label}",
                    flush=True,
                )
                started = time.perf_counter()
                try:
                    result = DeepSeekHarnessCodingAgent(
                        runtime, project_root=arguments.project_root
                    ).run(
                        workspace=workspace.path,
                        memory_tools=memory,
                        state_dir=state_dir,
                        harness_home=root / condition_lock["harness_home_relative"],
                        filesystem_root=root,
                        task=task.prompt,
                        repository=task.repository_id,
                        run_id=run_id,
                        task_id=task.id,
                        condition=condition_text,
                        cache_phase=CachePhase.COLD,
                        cache_namespace=hashlib.sha256(
                            f"{lock['original_run_id']}:{task.id}:{condition_text}".encode()
                        ).hexdigest(),
                        budget_tokens=1200,
                        resume_session_id=condition_lock["session_id"],
                        prompt_override=prompt,
                        enforce_budget=not arguments.single_natural_run,
                    )
                finally:
                    if memory is not None:
                        memory.close()
                latency = round(time.perf_counter() - started, 6)
                if result.status is AgentRunStatus.EXTERNAL_BLOCKER:
                    raise RuntimeError(f"{condition_text} external blocker: {result.message}")
                result_usage = list(result.usage)
                result_events = list(result.tool_events)
                provider_attempts = result.provider_attempts
                message = result.message
                agent_status = result.status.value
                agent_failure_reason = result.failure_reason
            if not result_usage:
                raise RuntimeError(
                    f"{condition_text} continuation produced no accounted response: "
                    f"{agent_failure_reason}: {message}"
                )
            known_usage = {_usage_key(item) for item in continuation_usage}
            for item in result_usage:
                if _usage_key(item) in known_usage:
                    continue
                continuation_usage.append(item)
                known_usage.add(_usage_key(item))
                _append_jsonl(usage_path, item.model_dump(mode="json"))
            known_events = {_event_key(item) for item in continuation_events}
            for item in result_events:
                if _event_key(item) in known_events:
                    continue
                continuation_events.append(item)
                known_events.add(_event_key(item))
                _append_jsonl(events_path, item.model_dump(mode="json"))

            _remove_stale_git_index_lock(workspace.path, condition_text)
            patch_path = output / "patches" / f"round-{round_index:03d}__{condition_text}.patch"
            patch = manager.capture_patch(workspace, patch_path)
            scoring_manager = RepositoryWorkspaceManager(
                campaign_root / "continuation-scoring" / condition_text / "repositories",
                refresh_existing_cache=False,
                include_condition_in_workspace_path=False,
            )
            score_run_id = _available_run_id(
                scoring_manager,
                f"{lock['continuation_run_prefix']}-score-r{round_index:03d}-{condition_text}",
            )
            scoring = scoring_manager.materialize(
                prepared,
                task,
                cast(ExperimentCondition, condition),
                run_id=score_run_id,
            )
            scoring_manager.apply_captured_patch(scoring, patch)
            docker_campaign_root = Path(lock["docker_host_campaign_root"])

            def resolve_bind(
                path: Path,
                *,
                _root: Path = campaign_root,
                _docker: Path = docker_campaign_root,
            ) -> Path:
                return _docker / path.resolve(strict=True).relative_to(_root)

            hidden_output = _available_path(
                output / "hidden-tests" / f"round-{round_index:03d}__{condition_text}"
            )
            hidden = HiddenTestRunner(scoring_manager, bind_source_resolver=resolve_bind).run(
                scoring,
                task.hidden_test,
                hidden_root=arguments.hidden_root,
                output_dir=hidden_output,
            )
            hidden_path = output / "results" / f"round-{round_index:03d}__{condition_text}.json"
            _write_json(hidden_path, hidden.as_dict())
            round_record = {
                "round": round_index,
                "condition": condition_text,
                "session_id": condition_lock["session_id"],
                "run_id": run_id,
                "agent_status": agent_status,
                "agent_failure_reason": agent_failure_reason,
                "provider_attempts": provider_attempts,
                "completed_requests": len(result_usage),
                "input_tokens": sum(item.input_tokens for item in result_usage),
                "output_tokens": sum(item.output_tokens for item in result_usage),
                "cost_usd": str(aggregate_usage(result_usage).cost_usd),
                "latency_seconds": latency,
                "latency_source": "filesystem_recovery" if recovered else "measured",
                "memory_tool_calls": len(result_events),
                "patch_sha256": patch.sha256,
                "patch_bytes": patch.size_bytes,
                "changed_files": list(patch.changed_files),
                "hidden_test_success": hidden.success,
                "hidden_actual_exit_code": hidden.actual_exit_code,
                "hidden_setup_error_code": hidden.setup_error_code,
                "message": message,
            }
            rounds.append(round_record)
            _append_jsonl(rounds_path, round_record)
            print(
                f"condition_result round={round_index} condition={condition_text} "
                f"attempts={provider_attempts} responses={len(result_usage)} "
                f"patch_bytes={patch.size_bytes} hidden_pass={str(hidden.success).lower()} "
                f"latency_seconds={latency}",
                flush=True,
            )
            if hidden.success:
                solved.add(condition_text)
            elif arguments.single_natural_run:
                raise RuntimeError(
                    f"{condition_text} natural continuation ended before frozen acceptance passed"
                )

    report = {
        "schema_version": "1.0",
        "status": "completed",
        "finished_at": datetime.now(UTC).isoformat(),
        "original_run_id": lock["original_run_id"],
        "task_id": task.id,
        "selected_conditions": selected_conditions,
        "same_sessions_resumed": True,
        "same_workspace_continued": True,
        "frozen_hidden_test_sha256": task.hidden_test.hidden_patch_sha256,
        "rounds": rounds,
        "conditions": {
            condition: _condition_summary(
                condition,
                initial_records=initial_records,
                initial_usage=initial_usage,
                initial_events=initial_events,
                rounds=rounds,
                continuation_usage=continuation_usage,
                continuation_events=continuation_events,
            )
            for condition in selected_conditions
        },
    }
    _write_json(output / "report.json", report)
    (output / "report.md").write_text(_render_report(report), encoding="utf-8", newline="\n")
    print("continuation_complete selected_conditions_passed=true", flush=True)


if __name__ == "__main__":
    main()
