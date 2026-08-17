from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from memoryos.evaluation.context_efficiency_runner import load_context_efficiency_runtime
from memoryos.evaluation.deepseek_harness_agent import (
    MEMORYOS_PLUGIN_VERSION,
    DeepSeekHarnessRuntime,
)
from memoryos.evaluation.openai_compatible_coding_agent import AgentRunStatus
from memoryos.evaluation.provider_usage import ProviderUsageRecord
from scripts.run_cross_session_memory_v1 import (
    MemoryOSProcess,
    RemoteMemoryBackend,
    TurnRun,
    _prepare_workspace,
    _run_turn,
    _semantic_memories,
    _source_write_token_summary,
    _usage_summary,
    _write_json,
)

_UNKNOWN = re.compile(r"不知道|无法(?:确认|得知|判断)|没有.{0,10}(?:依据|上下文|信息)", re.I)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fixture_root(project_root: Path, fixture_id: str) -> Path:
    return (
        project_root
        / "benchmarks"
        / "context_efficiency"
        / "cross_session_memory_v1"
        / "fixtures"
        / fixture_id
    )


def _workspace_snapshot(workspace: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {
        "head": head,
        "status": status,
        "clean": not status.strip(),
    }


def _mutation_score(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "head_unchanged": before["head"] == after["head"],
        "worktree_clean_before": before["clean"],
        "worktree_clean_after": after["clean"],
        "status_after": after["status"],
        "passed": bool(before["head"] == after["head"] and before["clean"] and after["clean"]),
    }


def _backend(
    service: MemoryOSProcess,
    *,
    prompt: str,
    repository: str,
) -> RemoteMemoryBackend:
    base_url, token = service.endpoint()
    return RemoteMemoryBackend(
        base_url=base_url,
        token=token,
        task=prompt,
        repository=repository,
        budget_tokens=1200,
    )


def _turn(
    *,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    root: Path,
    workspace: Path,
    prompt: str,
    repository: str,
    campaign_id: str,
    task_id: str,
    logical_session: str,
    turn: int,
    backend: RemoteMemoryBackend | None,
    resume_session_id: str | None = None,
    write_profile: bool = False,
    history_char_limit: int | None = None,
    sentinel: str | None = None,
) -> TurnRun:
    result = _run_turn(
        runtime=runtime,
        project_root=project_root,
        filesystem_root=root,
        workspace=workspace,
        prompt=prompt,
        repository=repository,
        run_id=f"{campaign_id}-{task_id}",
        task_id=task_id,
        logical_session=logical_session,
        turn=turn,
        backend=backend,
        resume_session_id=resume_session_id,
        write_profile=write_profile,
        evaluation_history_char_limit=history_char_limit,
        evaluation_sentinel=sentinel,
    )
    print(
        f"turn_complete task={task_id} status={result.status} "
        f"attempts={result.provider_attempts} memory_calls={len(result.tool_events)} "
        f"evictions={len(result.controlled_evictions)}",
        flush=True,
    )
    return result


def _restart(service: MemoryOSProcess, repository: str, label: str) -> dict[str, Any]:
    before = service.list_memories(repository, status=None)
    old_pid = service.process.pid if service.process is not None else None
    stop = service.stop()
    service.start()
    new_pid = service.process.pid if service.process is not None else None
    after = service.list_memories(repository, status=None)
    semantic_before = _semantic_memories(before)
    semantic_after = _semantic_memories(after)
    result = {
        "label": label,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "old_process_exit_confirmed": stop.get("exit_confirmed") is True,
        "process_id_changed": old_pid is not None and new_pid is not None and old_pid != new_pid,
        "persistent_data_directory_preserved": True,
        "memories_before": semantic_before,
        "memories_after": semantic_after,
        "memory_semantics_preserved": semantic_before == semantic_after,
        "transcript_reused": False,
        "harness_home_reused": False,
    }
    result["passed"] = bool(
        result["old_process_exit_confirmed"]
        and result["process_id_changed"]
        and result["memory_semantics_preserved"]
    )
    return result


def _memory_text(memory: dict[str, Any]) -> str:
    return f"{memory.get('title', '')}\n{memory.get('content', '')}"


def _matching_memories(memories: list[dict[str, Any]], *terms: str) -> list[dict[str, Any]]:
    folded_terms = [term.casefold() for term in terms]
    return [
        item
        for item in memories
        if all(term in _memory_text(item).casefold() for term in folded_terms)
    ]


def _tool_names(run: TurnRun) -> list[str]:
    return [event.tool for event in run.tool_events]


def _service_events(run: TurnRun, tool: str) -> list[dict[str, Any]]:
    return [event for event in run.remote_events if event.get("tool") == tool]


def _run_update_test(
    *,
    case: dict[str, Any],
    campaign: Path,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    service: MemoryOSProcess,
    campaign_id: str,
) -> dict[str, Any]:
    repository = str(case["repository_scope"])
    fixture = _fixture_root(project_root, str(case["fixture_id"]))
    roots = {
        label: campaign / "session-roots" / f"memory-update__{label.lower()}"
        for label in ("A", "B", "C")
    }
    workspaces = {label: _prepare_workspace(fixture, root) for label, root in roots.items()}
    before = {label: _workspace_snapshot(workspace) for label, workspace in workspaces.items()}

    prompt_a = str(case["session_a"]["prompt"])
    run_a = _turn(
        runtime=runtime,
        project_root=project_root,
        root=roots["A"],
        workspace=workspaces["A"],
        prompt=prompt_a,
        repository=repository,
        campaign_id=campaign_id,
        task_id="memory-update-a",
        logical_session=f"{campaign_id}:memory-update:A",
        turn=1,
        backend=_backend(service, prompt=prompt_a, repository=repository),
        write_profile=True,
    )
    after_a = service.list_memories(repository, status=None)
    restart_ab = _restart(service, repository, "A_to_B")

    prompt_b = str(case["session_b"]["prompt"])
    run_b = _turn(
        runtime=runtime,
        project_root=project_root,
        root=roots["B"],
        workspace=workspaces["B"],
        prompt=prompt_b,
        repository=repository,
        campaign_id=campaign_id,
        task_id="memory-update-b",
        logical_session=f"{campaign_id}:memory-update:B",
        turn=1,
        backend=_backend(service, prompt=prompt_b, repository=repository),
        write_profile=True,
    )
    after_b = service.list_memories(repository, status=None)
    restart_bc = _restart(service, repository, "B_to_C")

    prompt_c = str(case["session_c"]["prompt"])
    run_c = _turn(
        runtime=runtime,
        project_root=project_root,
        root=roots["C"],
        workspace=workspaces["C"],
        prompt=prompt_c,
        repository=repository,
        campaign_id=campaign_id,
        task_id="memory-update-c",
        logical_session=f"{campaign_id}:memory-update:C",
        turn=1,
        backend=_backend(service, prompt=prompt_c, repository=repository),
    )
    final_memories = service.list_memories(repository, status=None)
    after = {label: _workspace_snapshot(workspace) for label, workspace in workspaces.items()}

    old_after_a = _matching_memories(after_a, "postgresql", "17")
    old_ids = {str(item["id"]) for item in old_after_a}
    old_keys = {str(item.get("key") or "") for item in old_after_a}
    # The replacement memory is allowed to say that PostgreSQL 17 is no longer
    # current.  Identify the old truth by its stable record identity rather than
    # treating that historical mention as an active PostgreSQL 17 assertion.
    old_final = [item for item in final_memories if str(item.get("id")) in old_ids]
    new_final = [
        item
        for item in _matching_memories(final_memories, "postgresql", "18")
        if str(item.get("id")) not in old_ids
        and (not old_keys or str(item.get("key") or "") in old_keys)
    ]
    old_active = [item for item in old_final if item.get("status") == "active"]
    old_superseded = [item for item in old_final if item.get("status") == "superseded"]
    new_active = [item for item in new_final if item.get("status") == "active"]
    strategy_events = [
        event
        for event in _service_events(run_b, "memory_confirm")
        if event.get("safe_arguments", {}).get("strategy") == "supersede"
        and event.get("ok") is True
    ]
    conflict_events = [
        event
        for event in _service_events(run_b, "memory_confirm")
        if event.get("ok") is False
        and ("409" in _canonical(event) or "CONFLICT" in _canonical(event).upper())
    ]
    context_events = [
        event for event in _service_events(run_c, "memory_context") if event.get("ok")
    ]
    context_text = "\n".join(_canonical(event.get("result")) for event in context_events)
    output_c = run_c.output.strip()
    unique_sessions = {
        run_a.session_id,
        run_b.session_id,
        run_c.session_id,
    }
    unique_sessions.discard(None)
    mutation = {label: _mutation_score(before[label], after[label]) for label in ("A", "B", "C")}

    gates = {
        "session_a_completed": run_a.status == AgentRunStatus.COMPLETED.value,
        "session_a_wrote_postgresql_17": bool(old_after_a)
        and any(item.get("status") == "active" for item in old_after_a),
        "hard_restart_a_to_b": restart_ab["passed"],
        "session_b_completed": run_b.status == AgentRunStatus.COMPLETED.value,
        "one_replacement_candidate": len(_service_events(run_b, "memory_propose")) == 1,
        "supersede_resolution_path_used": bool(conflict_events or strategy_events),
        "supersede_strategy_used": len(strategy_events) == 1,
        "old_truth_not_active": not old_active,
        "old_truth_superseded": bool(old_superseded),
        "new_truth_active": len(new_active) == 1,
        "supersede_link_points_to_old_truth": bool(
            new_active
            and old_superseded
            and new_active[0].get("supersedes_id") == old_superseded[0].get("id")
        ),
        "hard_restart_b_to_c": restart_bc["passed"],
        "three_fresh_agent_sessions": len(unique_sessions) == 3,
        "session_c_completed": run_c.status == AgentRunStatus.COMPLETED.value,
        "session_c_called_memory_context": "memory_context" in _tool_names(run_c),
        "current_context_contains_18": "PostgreSQL 18".casefold() in context_text.casefold(),
        "current_context_excludes_superseded_17_record": bool(old_final)
        and all(str(item["id"]) not in context_text for item in old_final),
        "session_c_answer_is_18_only": "postgresql" in output_c.casefold()
        and "18" in output_c
        and "17" not in output_c,
        "no_workspace_mutation": all(item["passed"] for item in mutation.values()),
    }
    return {
        "case_id": case["case_id"],
        "passed": all(gates.values()),
        "gates": gates,
        "sessions": {
            "A": run_a.report(),
            "B": run_b.report(),
            "C": run_c.report(),
        },
        "restarts": [restart_ab, restart_bc],
        "memory_state": {
            "after_a": after_a,
            "after_b": after_b,
            "final": final_memories,
            "old_current_truth_active_count": len(old_active),
            "old_current_truth_superseded_count": len(old_superseded),
            "new_current_truth_active_count": len(new_active),
        },
        "conflict_evidence": {
            "conflict_events": conflict_events,
            "supersede_events": strategy_events,
            "proposal_count": len(_service_events(run_b, "memory_propose")),
            "confirmation_count": len(_service_events(run_b, "memory_confirm")),
        },
        "workspace_mutation": mutation,
        "write_turns": {
            "memory_update_session_a": run_a,
            "memory_update_session_b": run_b,
        },
        "all_turns": [run_a, run_b, run_c],
    }


def _render_filler(case: dict[str, Any], batch: int) -> str:
    spec = case["filler"]
    components = list(spec["components"])
    topics = list(spec["topics"])
    observations = list(spec["observations"])
    lines = [str(spec["prompt_prefix"]).format(batch=batch)]
    for offset in range(int(spec["records_per_batch"])):
        index = offset + 1
        lines.append(
            str(spec["record_template"]).format(
                batch=batch,
                index=index,
                component=components[(offset + batch - 1) % len(components)],
                topic=topics[(offset * 5 + batch - 1) % len(topics)],
                observation=observations[(offset * 7 + batch - 1) % len(observations)],
            )
        )
    return "\n".join(lines)


def _run_eviction_arm(
    *,
    arm: Literal["no_memory", "memoryos"],
    case: dict[str, Any],
    campaign: Path,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    service: MemoryOSProcess,
    campaign_id: str,
) -> dict[str, Any]:
    repository = str(case["repository_scope"])
    root = campaign / "session-roots" / f"context-eviction__{arm}"
    fixture = _fixture_root(project_root, str(case["fixture_id"]))
    workspace = _prepare_workspace(fixture, root)
    before = _workspace_snapshot(workspace)
    logical_session = f"{campaign_id}:context-eviction:{arm}"
    limit = int(case["history_char_limit"])
    sentinel = str(case["sentinel"])
    turns: list[TurnRun] = []
    session_id: str | None = None

    source_prompt = str(case["session_a_prompt"])
    source_backend = (
        _backend(service, prompt=source_prompt, repository=repository)
        if arm == "memoryos"
        else None
    )
    source = _turn(
        runtime=runtime,
        project_root=project_root,
        root=root,
        workspace=workspace,
        prompt=source_prompt,
        repository=repository,
        campaign_id=campaign_id,
        task_id=f"context-eviction-{arm}-source",
        logical_session=logical_session,
        turn=1,
        backend=source_backend,
        write_profile=arm == "memoryos",
        history_char_limit=limit,
        sentinel=sentinel,
    )
    turns.append(source)
    session_id = source.session_id

    for batch in range(1, int(case["filler"]["batch_count"]) + 1):
        prompt = _render_filler(case, batch)
        filler = _turn(
            runtime=runtime,
            project_root=project_root,
            root=root,
            workspace=workspace,
            prompt=prompt,
            repository=repository,
            campaign_id=campaign_id,
            task_id=f"context-eviction-{arm}-filler-{batch}",
            logical_session=logical_session,
            turn=batch + 1,
            backend=None,
            resume_session_id=session_id,
            history_char_limit=limit,
            sentinel=sentinel,
        )
        turns.append(filler)

    final_prompt = str(case["final_prompt"])
    final_backend = (
        _backend(service, prompt=final_prompt, repository=repository) if arm == "memoryos" else None
    )
    final = _turn(
        runtime=runtime,
        project_root=project_root,
        root=root,
        workspace=workspace,
        prompt=final_prompt,
        repository=repository,
        campaign_id=campaign_id,
        task_id=f"context-eviction-{arm}-final",
        logical_session=logical_session,
        turn=int(case["filler"]["batch_count"]) + 2,
        backend=final_backend,
        resume_session_id=session_id,
        history_char_limit=limit,
        sentinel=sentinel,
    )
    turns.append(final)
    after = _workspace_snapshot(workspace)
    evictions = [event for turn in turns for event in turn.controlled_evictions]
    target_evictions = [
        event
        for event in evictions
        if event.get("shadowed_contains_sentinel") is True
        and event.get("retained_contains_sentinel") is False
    ]
    context_events = [
        event for event in _service_events(final, "memory_context") if event.get("ok") is True
    ]
    context_text = "\n".join(_canonical(event.get("result")) for event in context_events)
    same_session = len({turn.session_id for turn in turns}) == 1 and session_id is not None
    settings = (root / "home" / "settings.yaml").read_text(encoding="utf-8")
    source_memory = service.list_memories(repository, status=None) if arm == "memoryos" else []
    output = final.output.strip()
    gates = {
        "all_turns_completed": all(turn.status == AgentRunStatus.COMPLETED.value for turn in turns),
        "single_continuous_session": same_session,
        "configured_context_window_applied": (
            f"contextWindow: {case['configured_context_window_tokens']}" in settings
        ),
        "controlled_eviction_occurred": bool(evictions),
        "original_decision_shadowed": bool(target_evictions),
        "retained_history_excludes_sentinel": bool(target_evictions),
        "workspace_unmodified": _mutation_score(before, after)["passed"],
    }
    if arm == "no_memory":
        gates.update(
            {
                "memoryos_tools_absent_on_final_turn": not final.tool_events,
                "baseline_abstained": bool(_UNKNOWN.search(output)),
                "baseline_did_not_recover_sentinel": sentinel.casefold() not in output.casefold(),
            }
        )
    else:
        gates.update(
            {
                "source_memory_confirmed": any(
                    item.get("status") == "active"
                    and sentinel.casefold() in _memory_text(item).casefold()
                    for item in source_memory
                ),
                "memory_context_called_only_at_final_recall": (
                    "memory_context" in _tool_names(final)
                    and all("memory_context" not in _tool_names(turn) for turn in turns[1:-1])
                ),
                "memory_context_returned_sentinel": sentinel.casefold() in context_text.casefold(),
                "memoryos_recovered_sentinel": sentinel.casefold() in output.casefold(),
            }
        )
    return {
        "arm": arm,
        "passed": all(gates.values()),
        "gates": gates,
        "session_id": session_id,
        "turns": [turn.report() for turn in turns],
        "final_output": output,
        "controlled_context_evictions": evictions,
        "target_evictions": target_evictions,
        "settings_sha256": _sha256_text(settings),
        "workspace_mutation": _mutation_score(before, after),
        "source_memories": source_memory,
        "source_turn": source,
        "all_turns": turns,
    }


def _run_eviction_test(
    *,
    case: dict[str, Any],
    campaign: Path,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    service: MemoryOSProcess,
    campaign_id: str,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="context-eviction") as pool:
        futures = {
            arm: pool.submit(
                _run_eviction_arm,
                arm=arm,
                case=case,
                campaign=campaign,
                runtime=runtime,
                project_root=project_root,
                service=service,
                campaign_id=campaign_id,
            )
            for arm in ("no_memory", "memoryos")
        }
        arms = {arm: future.result() for arm, future in futures.items()}
    no_memory = arms["no_memory"]
    memoryos = arms["memoryos"]
    cross_arm = {
        "distinct_sessions": no_memory["session_id"] != memoryos["session_id"],
        "same_frozen_prompts": [turn["prompt_sha256"] for turn in no_memory["turns"]]
        == [turn["prompt_sha256"] for turn in memoryos["turns"]],
        "same_context_window_policy": no_memory["settings_sha256"] == memoryos["settings_sha256"],
        "both_evicted_original_decision": bool(no_memory["target_evictions"])
        and bool(memoryos["target_evictions"]),
        "expected_answer_separation": bool(_UNKNOWN.search(no_memory["final_output"]))
        and str(case["sentinel"]).casefold() in memoryos["final_output"].casefold(),
    }
    return {
        "case_id": case["case_id"],
        "passed": no_memory["passed"] and memoryos["passed"] and all(cross_arm.values()),
        "context_window": {
            "configured_tokens": case["configured_context_window_tokens"],
            "history_char_limit": case["history_char_limit"],
            "policy": "deterministic-complete-turn-tail-eviction-v1",
            "native_summary_compaction_used": False,
            "reason": (
                "Avoid preserving the target fact through a second summarization-memory mechanism."
            ),
        },
        "cross_arm_gates": cross_arm,
        "arms": {
            "no_memory": {
                key: value
                for key, value in no_memory.items()
                if key not in {"source_turn", "all_turns"}
            },
            "memoryos": {
                key: value
                for key, value in memoryos.items()
                if key not in {"source_turn", "all_turns"}
            },
        },
        "write_turns": {
            "context_eviction_memoryos_session_a": memoryos["source_turn"],
        },
        "all_turns": [*no_memory["all_turns"], *memoryos["all_turns"]],
    }


def _token_accounting(write_turns: dict[str, TurnRun]) -> dict[str, Any]:
    per_session = {name: _source_write_token_summary([turn]) for name, turn in write_turns.items()}
    fields = (
        "write_tool_schema_tokens",
        "memory_write_visible_tokens",
        "provider_input_tokens",
    )
    totals: dict[str, Any] = {}
    for field in fields:
        values = [value[field] for value in per_session.values()]
        totals[field] = sum(int(value) for value in values if value is not None)
        totals[f"{field}_complete"] = all(value is not None for value in values)
    return {
        "definitions": {
            "write_tool_schema_tokens": (
                "Estimated static one-copy MemoryOS write-tool schema tokens for that "
                "write session."
            ),
            "memory_write_visible_tokens": (
                "Estimated cumulative MemoryOS write schema/result tokens visible across "
                "provider attempts."
            ),
            "provider_input_tokens": (
                "Provider-exact total input tokens for that source/write session."
            ),
        },
        "per_write_session": per_session,
        "totals": totals,
    }


def _observability(turns: list[TurnRun]) -> dict[str, Any]:
    usage: list[ProviderUsageRecord] = [record for turn in turns for record in turn.usage]
    attempts = sum(turn.provider_attempts for turn in turns)
    summary = _usage_summary(usage, attempts)
    summary["session_count"] = len({turn.session_id for turn in turns if turn.session_id})
    return summary


def _preflight(
    *,
    cases: dict[str, Any],
    update_runtime: DeepSeekHarnessRuntime,
    eviction_runtime: DeepSeekHarnessRuntime,
    campaign: Path,
    project_root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    if update_runtime.provider_retry_limit != 0 or eviction_runtime.provider_retry_limit != 0:
        errors.append("provider retries must be disabled")
    if update_runtime.context_length != 1_000_000:
        errors.append("memory update runtime must keep the normal 1M context catalog entry")
    configured = int(cases["context_eviction"]["configured_context_window_tokens"])
    if eviction_runtime.context_length != configured:
        errors.append("eviction runtime context length diverges from the frozen case")
    if eviction_runtime.agent_preset != "minimal" or update_runtime.agent_preset != "minimal":
        errors.append("both tests must use the minimal Harness preset")
    if not os.environ.get(update_runtime.api_key_environment):
        errors.append(f"{update_runtime.api_key_environment} is unavailable")
    forbidden = [str(value).casefold() for value in cases["source_prompt_forbidden_terms"]]
    prompts = [
        str(cases["memory_update"][name]["prompt"])
        for name in ("session_a", "session_b", "session_c")
    ] + [
        str(cases["context_eviction"]["session_a_prompt"]),
        str(cases["context_eviction"]["final_prompt"]),
    ]
    for index, prompt in enumerate(prompts, 1):
        hits = [term for term in forbidden if term in prompt.casefold()]
        if hits:
            errors.append(f"prompt {index} names a forbidden memory mechanism: {hits}")
    for case_name in ("memory_update", "context_eviction"):
        fixture = _fixture_root(project_root, str(cases[case_name]["fixture_id"]))
        if not fixture.is_dir() or not any(path.is_file() for path in fixture.rglob("*")):
            errors.append(f"fixture is unavailable: {fixture}")
    roots = (
        "memory-update__a",
        "memory-update__b",
        "memory-update__c",
        "context-eviction__no_memory",
        "context-eviction__memoryos",
    )
    for name in roots:
        root = campaign / "session-roots" / name
        if not root.is_dir() or not root.is_mount():
            errors.append(f"dedicated session mount is unavailable: {root}")
    filler_lengths = {
        str(batch): len(_render_filler(cases["context_eviction"], batch))
        for batch in range(1, int(cases["context_eviction"]["filler"]["batch_count"]) + 1)
    }
    if min(filler_lengths.values(), default=0) < 2_000:
        errors.append("each frozen filler batch must contain at least 2000 characters")
    plugin_package = json.loads(
        (project_root / "integrations" / "deepseek-harness-memoryos" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    if plugin_package.get("version") != MEMORYOS_PLUGIN_VERSION:
        errors.append("plugin package and Python adapter versions diverge")
    return {
        "passed": not errors,
        "errors": errors,
        "provider_requests": 0,
        "plugin_version": MEMORYOS_PLUGIN_VERSION,
        "filler_prompt_characters": filler_lengths,
        "context_window_mapping_verified": not any("context length" in item for item in errors),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    update = report["memory_update_test"]
    eviction = report["context_eviction_test"]
    tokens = report["write_token_accounting"]
    lines = [
        "# MemoryOS Long-Term Memory Follow-up E2E v1",
        "",
        f"Overall status: `{report['status']}`",
        "",
        "| Test | Result | Core observation |",
        "|---|---:|---|",
        f"| Memory Update | {'PASS' if update['passed'] else 'FAIL'} | "
        f"17 active={update['memory_state']['old_current_truth_active_count']}; "
        f"17 superseded={update['memory_state']['old_current_truth_superseded_count']}; "
        f"18 active={update['memory_state']['new_current_truth_active_count']} |",
        f"| Context Eviction | {'PASS' if eviction['passed'] else 'FAIL'} | "
        f"No Memory=`{eviction['arms']['no_memory']['final_output']}`; "
        f"MemoryOS=`{eviction['arms']['memoryos']['final_output']}` |",
        "",
        "## Memory Update gates",
        "",
        *[f"- [{'x' if passed else ' '}] {name}" for name, passed in update["gates"].items()],
        "",
        "## Context Eviction gates",
        "",
        *[
            f"- [{'x' if passed else ' '}] no_memory.{name}"
            for name, passed in eviction["arms"]["no_memory"]["gates"].items()
        ],
        *[
            f"- [{'x' if passed else ' '}] memoryos.{name}"
            for name, passed in eviction["arms"]["memoryos"]["gates"].items()
        ],
        *[
            f"- [{'x' if passed else ' '}] cross_arm.{name}"
            for name, passed in eviction["cross_arm_gates"].items()
        ],
        "",
        "## Write-token accounting",
        "",
        (
            "| Write session | write_tool_schema_tokens | "
            "memory_write_visible_tokens | provider_input_tokens |"
        ),
        "|---|---:|---:|---:|",
        *[
            f"| {name} | {value['write_tool_schema_tokens']} | "
            f"{value['memory_write_visible_tokens']} | {value['provider_input_tokens']} |"
            for name, value in tokens["per_write_session"].items()
        ],
        f"| **Total** | **{tokens['totals']['write_tool_schema_tokens']}** | "
        f"**{tokens['totals']['memory_write_visible_tokens']}** | "
        f"**{tokens['totals']['provider_input_tokens']}** |",
        "",
        "Write components use unicode-heuristic-v1 estimates; provider input is provider-exact.",
        "",
        "## Total provider observability",
        "",
        f"- Attempts: {report['observability']['provider_attempts']}",
        f"- Input tokens: {report['observability']['input_tokens']}",
        f"- Output tokens: {report['observability']['output_tokens']}",
        f"- Reasoning tokens: {report['observability']['reasoning_tokens']}",
        f"- Cost (USD): {report['observability']['cost_usd']}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MemoryOS long-term follow-up E2E v1")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--update-runtime", type=Path, required=True)
    parser.add_argument("--eviction-runtime", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--loader-smoke-only", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve(strict=True)
    campaign = args.campaign_root.resolve(strict=True)
    cases_path = args.cases.resolve(strict=True)
    report_path = campaign / "report.json"
    if report_path.exists():
        raise RuntimeError(f"refusing to reuse completed campaign: {campaign}")
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    update_runtime = load_context_efficiency_runtime(args.update_runtime.resolve(strict=True))
    eviction_runtime = load_context_efficiency_runtime(args.eviction_runtime.resolve(strict=True))
    if not isinstance(update_runtime, DeepSeekHarnessRuntime) or not isinstance(
        eviction_runtime, DeepSeekHarnessRuntime
    ):
        raise TypeError("both follow-up runtimes must use DeepSeek Harness")
    preflight = _preflight(
        cases=cases,
        update_runtime=update_runtime,
        eviction_runtime=eviction_runtime,
        campaign=campaign,
        project_root=project_root,
    )
    _write_json(campaign / "preflight.json", preflight)
    if not preflight["passed"]:
        raise RuntimeError("follow-up preflight failed: " + "; ".join(preflight["errors"]))
    if args.preflight_only:
        preflight_path = campaign / "preflight.json"
        print(
            f"preflight_complete status=pass provider_requests=0 path={preflight_path}",
            flush=True,
        )
        return
    if args.loader_smoke_only:
        root = campaign / "session-roots" / "memory-update__a"
        fixture = _fixture_root(project_root, str(cases["memory_update"]["fixture_id"]))
        workspace = _prepare_workspace(fixture, root)
        smoke = _turn(
            runtime=update_runtime,
            project_root=project_root,
            root=root,
            workspace=workspace,
            prompt="只回复 OK, 不要运行工具。",
            repository="fixture://loader-smoke",
            campaign_id="loader-smoke",
            task_id="loader-smoke",
            logical_session="loader-smoke",
            turn=1,
            backend=None,
        )
        plugin_loaded = "plugin tree failed to load" not in smoke.output.casefold()
        result = {
            "plugin_loaded": plugin_loaded,
            "provider_dispatch_reached": smoke.provider_attempts >= 1,
            "provider_requests_reached_external_network": 0,
            "status": smoke.status,
            "failure_reason": smoke.failure_reason,
            "output_tail": smoke.output[-2000:],
        }
        result["passed"] = bool(result["plugin_loaded"] and result["provider_dispatch_reached"])
        _write_json(campaign / "loader-smoke.json", result)
        if not result["passed"]:
            raise RuntimeError("offline Loader smoke test failed")
        print("loader_smoke_complete status=pass external_requests=0", flush=True)
        return

    campaign_id = "long-term-followup-v1-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    update_service = MemoryOSProcess(
        data_dir=campaign / "memory-stores" / "memory-update",
        log_dir=campaign / "service-logs" / "memory-update",
        project_root=project_root,
    )
    eviction_service = MemoryOSProcess(
        data_dir=campaign / "memory-stores" / "context-eviction",
        log_dir=campaign / "service-logs" / "context-eviction",
        project_root=project_root,
    )
    started_at = _now()
    try:
        update_service.start()
        eviction_service.start()
        print("phase_start tests=memory_update,context_eviction parallel=2", flush=True)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-term-followup") as pool:
            update_future = pool.submit(
                _run_update_test,
                case=cases["memory_update"],
                campaign=campaign,
                runtime=update_runtime,
                project_root=project_root,
                service=update_service,
                campaign_id=campaign_id,
            )
            eviction_future = pool.submit(
                _run_eviction_test,
                case=cases["context_eviction"],
                campaign=campaign,
                runtime=eviction_runtime,
                project_root=project_root,
                service=eviction_service,
                campaign_id=campaign_id,
            )
            update = update_future.result()
            eviction = eviction_future.result()
    finally:
        update_service.stop()
        eviction_service.stop()

    write_turns = {**update.pop("write_turns"), **eviction.pop("write_turns")}
    all_turns = [*update.pop("all_turns"), *eviction.pop("all_turns")]
    report = {
        "schema_version": "1.0",
        "benchmark_id": cases["benchmark_id"],
        "campaign_id": campaign_id,
        "status": "pass" if update["passed"] and eviction["passed"] else "fail",
        "started_at": started_at,
        "finished_at": _now(),
        "cases_sha256": _sha256_text(cases_path.read_text(encoding="utf-8")),
        "plugin_version": MEMORYOS_PLUGIN_VERSION,
        "preflight": preflight,
        "runtime": {
            "update": update_runtime.model_dump(mode="json"),
            "eviction": eviction_runtime.model_dump(mode="json"),
        },
        "memory_update_test": update,
        "context_eviction_test": eviction,
        "write_token_accounting": _token_accounting(write_turns),
        "observability": _observability(all_turns),
        "service_history": {
            "memory_update": update_service.history,
            "context_eviction": eviction_service.history,
        },
    }
    _write_json(report_path, report)
    (campaign / "report.md").write_text(_render_markdown(report), encoding="utf-8", newline="\n")
    print(
        f"campaign_complete status={report['status']} report={report_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
