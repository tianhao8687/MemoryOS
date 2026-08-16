from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.request import Request, urlopen

import pytest

from memoryos.evaluation.context_efficiency import (
    MEMORYOS_CONTEXT_CONDITIONS,
    ContextEfficiencyCondition,
)
from memoryos.evaluation.context_efficiency_runner import (
    ContextEfficiencyRunConfig,
    ContextEfficiencyRunner,
    load_context_efficiency_inputs,
    load_context_efficiency_runtime,
)
from memoryos.evaluation.context_efficiency_runtime import (
    ConditionPolicy,
    MemoryOSToolBackend,
)
from memoryos.evaluation.deepseek_harness_agent import (
    _BUDGET_RETURN_CODE,
    _TIMEOUT_RETURN_CODE,
    DeepSeekHarnessCodingAgent,
    DeepSeekHarnessRuntime,
    MemoryOSHTTPBridge,
    _deepseek_budget_probe,
    _deepseek_optimized_preset_text,
    _freeze_harness_request_controls,
    _freeze_harness_settings,
    _looks_external,
    _plugin_environment,
    _standard_offline_preset_text,
    _workspace_change_fingerprint,
    harness_headless_task,
)
from memoryos.evaluation.fixture_openai_server import reset_fixture_cache
from memoryos.evaluation.provider_usage import CachePhase, ProviderUsageRecord

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.v23
def test_warm_cache_phase_requires_a_paired_cold_run() -> None:
    with pytest.raises(ValueError, match="cold-then-warm"):
        ContextEfficiencyRunConfig(cache_phases=(CachePhase.WARM,))


@pytest.mark.v23
def test_runner_routes_baseline_to_a_separate_physical_root(tmp_path: Path) -> None:
    primary = tmp_path / "with-memoryos"
    baseline = tmp_path / "without-memoryos"
    runner = ContextEfficiencyRunner(
        primary,
        condition_work_roots={ContextEfficiencyCondition.NO_MEMORY: baseline},
    )

    assert runner.workspace_manager.root == (primary / "repositories").resolve()
    assert runner.condition_work_roots == {ContextEfficiencyCondition.NO_MEMORY: baseline.resolve()}
    assert (
        runner.condition_workspace_managers[ContextEfficiencyCondition.NO_MEMORY].root
        == (baseline / "repositories").resolve()
    )
    assert not runner.condition_workspace_managers[
        ContextEfficiencyCondition.NO_MEMORY
    ].include_condition_in_workspace_path
    assert runner.scoring_workspace_manager is runner.workspace_manager
    assert not runner.scoring_workspace_manager.root.is_relative_to(baseline.resolve())


@pytest.mark.v23
def test_runner_keeps_scoring_workspace_outside_agent_condition_root(
    tmp_path: Path,
) -> None:
    reset_fixture_cache()
    manifest, runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "manifest.json",
        ROOT / "runtime" / "context-efficiency-fixture.json",
    )
    controller_root = tmp_path / "controller"
    condition_root = tmp_path / "agent-condition"
    output = tmp_path / "output"
    ContextEfficiencyRunner(
        controller_root,
        condition_work_roots={
            ContextEfficiencyCondition.LEGACY_FULL: condition_root,
        },
    ).run(
        manifest,
        runtime,
        hidden_root=tmp_path / "hidden",
        output_root=output,
        run_id="pytest-scoring-isolation",
        config=ContextEfficiencyRunConfig(
            conditions=(ContextEfficiencyCondition.LEGACY_FULL,),
            cache_phases=(CachePhase.COLD,),
        ),
        task_limit=1,
    )

    agent_workspaces = list((condition_root / "repositories" / "runs").glob("**/workspace"))
    scoring_workspaces = list((controller_root / "repositories" / "runs").glob("**/workspace"))
    assert len(agent_workspaces) == 1
    assert len(scoring_workspaces) == 1
    assert "score" in scoring_workspaces[0].as_posix()
    assert not scoring_workspaces[0].is_relative_to(condition_root)


@pytest.mark.v23
def test_runner_rejects_unknown_exact_task_ids_before_creating_output(tmp_path: Path) -> None:
    manifest, runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "manifest.json",
        ROOT / "runtime" / "context-efficiency-fixture.json",
    )
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="unknown task_ids"):
        ContextEfficiencyRunner(tmp_path / "work").run(
            manifest,
            runtime,
            hidden_root=tmp_path / "hidden",
            output_root=output,
            run_id="pytest-exact-task",
            task_ids=("missing-task",),
        )
    assert not output.exists()


@pytest.mark.v23
def test_qwen_stress_pilot_freezes_three_repositories_and_two_sequences() -> None:
    manifest, _runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "qwen_pilot_v1" / "manifest.json",
        ROOT / "runtime" / "qwen3-vl-local.json",
    )
    grouped: defaultdict[str, list[Any]] = defaultdict(list)
    for task in manifest.tasks:
        grouped[task.sequence_id].append(task)

    multistep = [items for items in grouped.values() if len(items) > 1]
    assert len(manifest.repositories) == 3
    assert len(manifest.tasks) == 6
    assert len(multistep) >= 2
    for items in multistep:
        ordered = sorted(items, key=lambda item: item.sequence_index)
        assert len({item.repository_id for item in ordered}) == 1
        assert [item.sequence_index for item in ordered] == list(range(1, len(ordered) + 1))
        assert [item.cutoff for item in ordered] == sorted(item.cutoff for item in ordered)


@pytest.mark.v23
def test_qwen_calibrated_pilot_is_bounded_and_hash_locked() -> None:
    root = ROOT / "benchmarks" / "context_efficiency" / "qwen_calibrated_v1"
    manifest, runtime = load_context_efficiency_inputs(
        root / "manifest.json",
        ROOT / "runtime" / "qwen3-vl-calibrated.json",
    )
    grouped: defaultdict[str, list[Any]] = defaultdict(list)
    for task in manifest.tasks:
        grouped[task.sequence_id].append(task)
        hidden_patch = root / "hidden" / cast(str, task.hidden_test.hidden_patch)
        assert hashlib.sha256(hidden_patch.read_bytes()).hexdigest() == (
            task.hidden_test.hidden_patch_sha256
        )

    assert len(manifest.repositories) == 3
    assert len(manifest.tasks) == 6
    assert sorted(len(items) for items in grouped.values()) == [2, 2, 2]
    assert all(
        sorted(item.sequence_index for item in items) == [1, 2] for items in grouped.values()
    )
    assert runtime.max_steps == 12
    assert runtime.max_output_tokens_per_step == 1536
    assert len(runtime.allowed_tests) == 6

    before_after, _ = load_context_efficiency_inputs(
        root / "manifest-before-after.json",
        ROOT / "runtime" / "qwen3-vl-calibrated.json",
    )
    assert len(before_after.tasks) == 6
    assert all("memory_context" not in item.prompt for item in before_after.tasks)
    assert all("memory_explain" not in item.prompt for item in before_after.tasks)
    assert all("before-after" in item.tags for item in before_after.tasks)


@pytest.mark.v23
def test_five_condition_runner_executes_cold_warm_and_stable_requests(tmp_path: Path) -> None:
    reset_fixture_cache()
    manifest, runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "manifest.json",
        ROOT / "runtime" / "context-efficiency-fixture.json",
    )
    output = tmp_path / "output"
    summary = ContextEfficiencyRunner(tmp_path / "work").run(
        manifest,
        runtime,
        hidden_root=tmp_path / "hidden",
        output_root=output,
        run_id="pytest-five-condition",
        config=ContextEfficiencyRunConfig(
            conditions=MEMORYOS_CONTEXT_CONDITIONS,
            cache_phases=(CachePhase.COLD, CachePhase.WARM),
        ),
    )

    assert summary["status"] == "completed_fixture"
    assert summary["run_count"] == 10
    assert summary["external_blocker_count"] == 0
    expected_artifacts = {
        "run-manifest.json",
        "records.jsonl",
        "provider-usage.jsonl",
        "tool-events.jsonl",
        "patches",
        "test-results",
        "summary.json",
        "summary.md",
    }
    assert expected_artifacts <= {path.name for path in output.iterdir()}

    records = [
        json.loads(line)
        for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 10
    assert {record["status"] for record in records} == {"completed"}
    assert all(record["record"]["hidden_test_success"] for record in records)
    assert all(record["record"]["cross_project_leaks"] == 0 for record in records)
    for record in records:
        patch = (output / record["patch_path"]).read_text(encoding="utf-8")
        assert "return left + right" in patch

    grouped: defaultdict[tuple[str, str], list[ProviderUsageRecord]] = defaultdict(list)
    for line in (output / "provider-usage.jsonl").read_text(encoding="utf-8").splitlines():
        usage = ProviderUsageRecord.model_validate_json(line)
        grouped[(usage.condition, usage.cache_phase.value)].append(usage)
    for condition in MEMORYOS_CONTEXT_CONDITIONS:
        cold = sorted(grouped[(condition.value, "cold")], key=lambda item: item.step_index)
        warm = sorted(grouped[(condition.value, "warm")], key=lambda item: item.step_index)
        assert cold
        assert [item.request_sha256 for item in cold] == [item.request_sha256 for item in warm]
        assert [item.request_bytes for item in cold] == [item.request_bytes for item in warm]
        assert all(item.cache_hit_tokens == 0 for item in cold)
        assert all(item.cache_miss_tokens == item.input_tokens for item in cold)
        assert all(item.cache_hit_tokens == item.input_tokens for item in warm)
        assert all(item.cache_miss_tokens == 0 for item in warm)

    conditions = summary["conditions"]
    assert conditions["msc_progressive"]["cold"]["progressive_explain_calls"] == 1
    assert conditions["msc_progressive"]["warm"]["progressive_explain_calls"] == 1
    for condition_name in ("msc_delta", "msc_delta_core"):
        assert conditions[condition_name]["cold"]["delta_hits"] == 1
        assert conditions[condition_name]["warm"]["delta_hits"] == 1
        assert conditions[condition_name]["cold"]["full_fallbacks"] == 0
        assert conditions[condition_name]["warm"]["full_fallbacks"] == 0


@pytest.mark.v23
def test_before_after_runner_has_true_no_memory_baseline_and_paired_report(
    tmp_path: Path,
) -> None:
    reset_fixture_cache()
    manifest, runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "manifest.json",
        ROOT / "runtime" / "context-efficiency-fixture.json",
    )
    output = tmp_path / "output"
    summary = ContextEfficiencyRunner(tmp_path / "work").run(
        manifest,
        runtime,
        hidden_root=tmp_path / "hidden",
        output_root=output,
        run_id="pytest-before-after",
        config=ContextEfficiencyRunConfig(
            conditions=(
                ContextEfficiencyCondition.NO_MEMORY,
                ContextEfficiencyCondition.MSC_FULL,
                ContextEfficiencyCondition.MSC_CONTEXT_ONLY,
            ),
            cache_phases=(CachePhase.COLD,),
        ),
    )

    comparison = summary["before_after"]["comparisons"]["msc_context_only"]["cold"]
    assert summary["run_count"] == 3
    assert comparison["paired_tasks"] == 1
    assert comparison["integrity"]["identical_prompt_pairs"] == 1
    assert comparison["integrity"]["identical_starting_state_pairs"] == 1
    assert comparison["integrity"]["identical_runtime_pairs"] == 1
    assert comparison["integrity"]["baseline_memory_tool_calls"] == 0
    assert comparison["memory_tool_calls"]["before"] == 0
    assert comparison["memory_tool_calls"]["after"] > 0
    assert "msc_full" in summary["before_after"]["comparisons"]
    records = [
        json.loads(line)
        for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    baseline = next(item for item in records if item["record"]["condition"] == "no_memory")
    assert baseline["record"]["tool_profile"] == "none"
    assert baseline["record"]["memory_tool_calls"] == 0
    assert baseline["record"]["memory_context_text_tokens"] == 0


@pytest.mark.v23
def test_runtime_union_loads_qwen_and_pinned_harness_and_blocks_without_harness(
    tmp_path: Path,
) -> None:
    qwen = load_context_efficiency_runtime(ROOT / "runtime" / "qwen-local.json")
    harness = load_context_efficiency_runtime(ROOT / "runtime" / "deepseek-harness.json")
    assert qwen.adapter == "openai_compatible"
    assert isinstance(harness, DeepSeekHarnessRuntime)
    assert harness.harness_version == "0.1.0-rc.5"
    assert harness.harness_commit == "47f943859bef60e4160492346772ded9b24f765a"
    assert harness.execution_environment == "linux-docker-wsl2"
    assert harness.execution_image.startswith("memoryos-deepseek-lab@sha256:")
    assert harness.agent_preset == "minimal"
    assert harness.reasoning_effort == "max"
    assert harness.permission_mode == "danger-full-access"
    assert harness.effective_permission_mode == "dedicated-condition-mount-write"
    assert harness.sandbox_authority == "outer-landlock-plus-shell-seccomp"
    assert harness.tool_shell_network == "none"
    assert harness.tool_shell_read_scope == "dedicated-condition-mount-system-only"
    assert harness.tool_shell_launcher == "/opt/memoryos/bin/bash"
    assert harness.agent_read_scope == "dedicated-condition-mount-runtime-only"
    assert harness.agent_filesystem_root_policy == "must-be-dedicated-mountpoint"
    assert harness.agent_filesystem_launcher.endswith("/landlock-run")
    assert harness.pnpm_version == "11.7.0"
    assert harness.pricing.find("deepseek", harness.model) is not None

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = DeepSeekHarnessCodingAgent(harness, which=lambda _name: None).run(
        workspace=workspace,
        memory_tools=cast(Any, object()),
        state_dir=tmp_path / "state",
        harness_home=tmp_path / "home",
        filesystem_root=tmp_path,
        task="fixture",
        repository="fixture",
        run_id="harness-blocker",
        task_id="fixture-task",
        condition="msc_progressive",
        cache_phase=CachePhase.COLD,
        cache_namespace="a" * 64,
        budget_tokens=6000,
    )
    assert result.status.value == "external_blocker"
    assert result.failure_reason == "external_blocker"
    assert result.usage == ()


@pytest.mark.v23
def test_harness_timeout_is_a_scored_failure_and_kills_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "orphan-survived.txt"
    child = (
        "import pathlib,sys,time; "
        "time.sleep(2); pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )

    result = DeepSeekHarnessCodingAgent._command(
        [sys.executable, "-c", parent, str(marker)],
        cwd=tmp_path,
        environment=os.environ,
        timeout_seconds=1,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == _TIMEOUT_RETURN_CODE
    assert "process timed out after 1 seconds" in result.stderr
    assert not _looks_external(result)
    time.sleep(2)
    assert not marker.exists(), "timed-out Harness descendants must not survive"


@pytest.mark.v23
def test_deepseek_budget_preserves_a_patch_and_kills_descendants(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved git initializes only the temporary test workspace
        [git, "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "change.py").write_text("changed = True\n", encoding="utf-8")
    usage_path = tmp_path / "provider-usage.jsonl"
    usage_path.write_text(
        "".join(json.dumps({"input_tokens": 1}) + "\n" for _ in range(48)),
        encoding="utf-8",
    )
    marker = tmp_path / "budget-orphan-survived.txt"
    child = (
        "import pathlib,sys,time; "
        "time.sleep(2); pathlib.Path(sys.argv[1]).write_text('alive', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )

    result = DeepSeekHarnessCodingAgent._command(
        [sys.executable, "-c", parent, str(marker)],
        cwd=workspace,
        environment=os.environ,
        timeout_seconds=10,
        budget_probe=_deepseek_budget_probe(
            usage_path,
            workspace,
            progress_grace_requests=0,
        ),
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == _BUDGET_RETURN_CODE
    assert "patch-stagnation soft ceiling" in result.stderr
    time.sleep(2)
    assert not marker.exists(), "budget-stopped Harness descendants must not survive"


@pytest.mark.v23
def test_deepseek_budget_can_freeze_an_equal_hard_request_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    usage_path = tmp_path / "provider-usage.jsonl"
    usage_path.write_text(
        "".join(json.dumps({"input_tokens": 1}) + "\n" for _ in range(30)),
        encoding="utf-8",
    )

    reason = _deepseek_budget_probe(
        usage_path,
        workspace,
        patch_preserving_request_limit=30,
        patch_preserving_input_token_limit=5_000_000,
        hard_request_limit=30,
        hard_input_token_limit=5_000_000,
    )()

    assert reason == "hard ceiling; provider_attempts=30/30"


@pytest.mark.v23
def test_deepseek_budget_stops_read_only_exploration_before_hard_ceiling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    usage_path = tmp_path / "provider-usage.jsonl"
    usage_path.write_text(
        "".join(json.dumps({"input_tokens": 10_000}) + "\n" for _ in range(20)),
        encoding="utf-8",
    )

    reason = _deepseek_budget_probe(usage_path, workspace)()

    assert reason == "no-patch ceiling; provider_attempts=20/20"


@pytest.mark.v23
def test_deepseek_budget_caps_cumulative_output_and_extends_only_after_a_patch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [git, "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    usage_path = tmp_path / "provider-usage.jsonl"
    usage_path.write_text(
        "".join(
            json.dumps({"input_tokens": 1_000, "output_tokens": 8_000}) + "\n" for _ in range(4)
        ),
        encoding="utf-8",
    )
    limits = {
        "no_patch_request_limit": 18,
        "no_patch_input_token_limit": 400_000,
        "no_patch_output_token_limit": 32_000,
        "patch_preserving_request_limit": 24,
        "patch_preserving_input_token_limit": 600_000,
        "patch_preserving_output_token_limit": 48_000,
        "hard_request_limit": 24,
        "hard_input_token_limit": 600_000,
        "hard_output_token_limit": 48_000,
    }

    assert _deepseek_budget_probe(usage_path, workspace, **limits)() == (
        "no-patch ceiling; output_tokens=32000/32000"
    )

    (workspace / "implementation.py").write_text("changed = True\n", encoding="utf-8")
    assert _deepseek_budget_probe(usage_path, workspace, **limits)() is None


@pytest.mark.v23
def test_deepseek_budget_ignores_file_mode_only_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [git, "init", "--quiet"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    tracked = workspace / "setup.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [git, "add", "setup.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [git, "update-index", "--chmod=+x", "setup.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [
            git,
            "-c",
            "user.name=MemoryOS Test",
            "-c",
            "user.email=memoryos-test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603 - resolved git only touches the temporary fixture
        [git, "config", "core.fileMode", "true"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )

    assert _workspace_change_fingerprint(workspace) is None


@pytest.mark.v23
def test_deepseek_budget_counts_retry_attempts_against_the_absolute_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    usage_path = tmp_path / "provider-usage.jsonl"
    usage_path.write_text(
        "".join(json.dumps({"input_tokens": 1}) + "\n" for _ in range(30)),
        encoding="utf-8",
    )
    attempt_path = tmp_path / "provider-attempts.jsonl"
    attempt_path.write_text(
        "".join(
            json.dumps({"event": "provider_attempt", "attempt_index": index}) + "\n"
            for index in range(1, 61)
        ),
        encoding="utf-8",
    )

    reason = _deepseek_budget_probe(
        usage_path,
        workspace,
        attempt_path=attempt_path,
    )()

    assert reason == "hard ceiling; provider_attempts=60/60"


@pytest.mark.v23
def test_open_source_ab_runtime_freezes_bounded_matched_limits() -> None:
    runtime = load_context_efficiency_runtime(
        ROOT / "runtime" / "deepseek-harness-open-source-ab.json"
    )
    assert isinstance(runtime, DeepSeekHarnessRuntime)
    assert runtime.executable == "/opt/memoryos/bin/dsh"
    assert runtime.agent_preset == "deepseek-optimized-offline-v3"
    assert runtime.reasoning_effort == "high"
    assert runtime.max_output_tokens == 8192
    assert runtime.run_timeout_seconds == 1200
    assert runtime.provider_retry_limit == 1
    assert runtime.no_patch_request_limit == 20
    assert runtime.no_patch_input_token_limit == 800_000
    assert runtime.patch_preserving_request_limit == 30
    assert runtime.patch_preserving_input_token_limit == 1_500_000
    assert runtime.progress_grace_requests == 6
    assert runtime.hard_request_limit == 60
    assert runtime.hard_input_token_limit == 3_000_000
    assert runtime.effective_memory_budget_tokens(6000) == 512
    assert runtime.effective_memory_budget_tokens(256) == 256


@pytest.mark.v23
def test_full_plugin_acceptance_is_neutral_hash_locked_and_bounded() -> None:
    root = ROOT / "benchmarks" / "context_efficiency" / "full_plugin_acceptance_v1"
    manifest, runtime = load_context_efficiency_inputs(
        root / "manifest.json",
        ROOT / "runtime" / "deepseek-harness-full-acceptance.json",
    )
    compact = load_context_efficiency_runtime(
        ROOT / "runtime" / "deepseek-harness-full-acceptance-compact.json"
    )
    lock = json.loads((root / "acceptance-lock.json").read_text(encoding="utf-8"))

    assert isinstance(runtime, DeepSeekHarnessRuntime)
    assert isinstance(compact, DeepSeekHarnessRuntime)
    assert len(manifest.tasks) == 1
    task = manifest.tasks[0]
    assert task.id == "operations-release-decision"
    assert task.base_commit == "abe2e744bd62d4c55624a6d3b2b18d4671e7262e"
    assert "memoryos" not in task.prompt.lower()
    assert "memory_context" not in task.prompt.lower()
    assert "memory_explain" not in task.prompt.lower()
    assert "non-code" in task.tags
    assert lock["prompt_mentions_memoryos"] is False
    assert lock["core_conditions"] == [
        "no_memory",
        "legacy_full",
        "msc_full",
        "msc_progressive",
        "msc_delta",
        "msc_delta_core",
    ]
    assert runtime.agent_preset == "standard-offline"
    assert compact.agent_preset == "deepseek-optimized-offline-v3"
    for selected in (runtime, compact):
        assert selected.reasoning_effort == "high"
        assert selected.run_timeout_seconds == 300
        assert selected.no_patch_request_limit == 7
        assert selected.patch_preserving_request_limit == 9
        assert selected.hard_request_limit == 10
        assert selected.progress_grace_requests == 1
    assert runtime.effective_memory_budget_tokens(1200) == 1200
    assert compact.effective_memory_budget_tokens(1200) == 512


@pytest.mark.v23
def test_harness_settings_freeze_model_preset_and_reasoning(tmp_path: Path) -> None:
    runtime = load_context_efficiency_runtime(ROOT / "runtime" / "deepseek-harness.json")
    assert isinstance(runtime, DeepSeekHarnessRuntime)
    home = tmp_path / "home"
    home.mkdir()

    _freeze_harness_settings(home, runtime)
    expected = (
        "agent-presets:\n"
        "  default: minimal\n"
        "agent-default-model:\n"
        "  provider: deepseek-official\n"
        "  model: deepseek-v4-flash\n"
        "  reasoningEffort: max\n"
        "llm-deepseek:\n"
        "  maxTokens: 256000\n"
        "  retryPolicy:\n"
        "    mode: normal\n"
        "    maxRetries: 1\n"
    )
    assert (home / "settings.yaml").read_text(encoding="utf-8") == expected
    _freeze_harness_settings(home, runtime)

    (home / "settings.yaml").write_text(
        expected.replace("default: minimal", "default: standard"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="settings diverge"):
        _freeze_harness_settings(home, runtime)


@pytest.mark.v23
def test_harness_request_controls_stabilize_persona_and_disable_title_llm(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    _freeze_harness_request_controls(home)
    expected = (
        "# Keep auxiliary presentation work out of provider-request accounting.\n"
        "# Keep the global headless persona independent of the disposable workspace path.\n"
        "- id: system-prompt\n"
        "  config:\n"
        "    persona: >-\n"
        "      You are a coding agent powered by the {{model}} model. "
        "Work only in the assigned isolated workspace.\n"
        "\n"
        "- id: session-title-llm\n"
        "  disabled: true\n"
    )
    assert (home / "cordis.patch.yml").read_text(encoding="utf-8") == expected
    _freeze_harness_request_controls(home)

    (home / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="request-control patch diverges"):
        _freeze_harness_request_controls(home)


@pytest.mark.v23
def test_standard_harness_runtime_changes_only_the_agent_preset() -> None:
    minimal = load_context_efficiency_runtime(ROOT / "runtime" / "deepseek-harness.json")
    standard = load_context_efficiency_runtime(ROOT / "runtime" / "deepseek-harness-standard.json")
    standard_offline = load_context_efficiency_runtime(
        ROOT / "runtime" / "deepseek-harness-standard-offline.json"
    )
    optimized = load_context_efficiency_runtime(
        ROOT / "runtime" / "deepseek-harness-optimized.json"
    )
    assert isinstance(minimal, DeepSeekHarnessRuntime)
    assert isinstance(standard, DeepSeekHarnessRuntime)
    assert isinstance(standard_offline, DeepSeekHarnessRuntime)
    assert isinstance(optimized, DeepSeekHarnessRuntime)
    assert minimal.agent_preset == "minimal"
    assert standard.agent_preset == "standard"
    assert standard_offline.agent_preset == "standard-offline"
    assert optimized.agent_preset == "deepseek-optimized-offline-v3"
    assert {
        key: value
        for key, value in minimal.model_dump(mode="json").items()
        if key != "agent_preset"
    } == {
        key: value
        for key, value in standard.model_dump(mode="json").items()
        if key != "agent_preset"
    }
    assert {
        key: value
        for key, value in minimal.model_dump(mode="json").items()
        if key != "agent_preset"
    } == {
        key: value
        for key, value in standard_offline.model_dump(mode="json").items()
        if key != "agent_preset"
    }
    assert {
        key: value
        for key, value in minimal.model_dump(mode="json").items()
        if key != "agent_preset"
    } == {
        key: value
        for key, value in optimized.model_dump(mode="json").items()
        if key != "agent_preset"
    }


@pytest.mark.v23
def test_standard_offline_preset_removes_web_and_delegation() -> None:
    source = """# prefix
- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    text: >-
      You are a coding agent powered by the {{model}} model. Your working directory is {{cwd}}.
# ── delegation and workflows section
- id: delegation
  name: cordis:group
# ── remaining model-facing rows section
- id: tool-todo
  name: '@deepseek-ai/dsh-tool-todo'
# The `web` service and its search provider stay in the host composition
- id: tool-web
  name: '@deepseek-ai/dsh-tool-web'
"""

    rendered = _standard_offline_preset_text(source)

    assert "tool-todo" in rendered
    assert "delegation" not in rendered
    assert "tool-web" not in rendered
    assert "assigned isolated workspace" in rendered
    assert "Your working directory is {{cwd}}" not in rendered


@pytest.mark.v23
def test_deepseek_optimized_preset_keeps_coding_tools_and_removes_orchestration() -> None:
    source = """# prefix
- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    text: >-
      You are a coding agent powered by the {{model}} model. Your working directory is {{cwd}}.
- id: tool-bash
  name: '@deepseek-ai/dsh-tool-bash'
- id: tool-fs
  name: '@deepseek-ai/dsh-tool-fs'
- id: tool-fs-search
  name: '@deepseek-ai/dsh-tool-fs-search'
# ── background jobs section
- id: tool-jobs
  name: '@deepseek-ai/dsh-tool-jobs'
- id: tool-skill
  name: '@deepseek-ai/dsh-tool-skill'
- id: tool-goal
  name: '@deepseek-ai/dsh-tool-goal'
- id: plan-mode
  name: '@deepseek-ai/dsh-plan-mode'
# ── compaction section
- id: compaction-basic
  name: '@deepseek-ai/dsh-compaction-basic'
- id: tool-result-pruner
  config:
    thresholdChars: 8192
    headChars: 4096
    tailChars: 1024
# ── delegation and workflows section
- id: delegation
  name: '@deepseek-ai/dsh-tool-subagent'
# ── remaining model-facing rows section
- id: tool-ask-user
  name: '@deepseek-ai/dsh-tool-ask-user'
- id: tool-todo
  name: '@deepseek-ai/dsh-tool-todo'
# The `web` service and its search provider stay in the host composition
- id: tool-web
  name: '@deepseek-ai/dsh-tool-web'
"""

    rendered = _deepseek_optimized_preset_text(source)

    assert "concise coding agent" in rendered
    assert "assigned isolated workspace" in rendered
    assert "Your working directory is {{cwd}}" not in rendered
    assert "checked-out repository" in rendered
    assert "@deepseek-ai/dsh-tool-bash" in rendered
    assert "@deepseek-ai/dsh-tool-fs-search" in rendered
    assert "@deepseek-ai/dsh-compaction-basic" in rendered
    assert "thresholdChars: 4096" in rendered
    assert "headChars: 2048" in rendered
    assert "tailChars: 512" in rendered
    assert "tool-jobs" not in rendered
    assert "tool-skill" not in rendered
    assert "tool-goal" not in rendered
    assert "plan-mode" not in rendered
    assert "tool-ask-user" not in rendered
    assert "tool-todo" not in rendered
    assert "tool-subagent" not in rendered
    assert "tool-web" not in rendered


@pytest.mark.v23
def test_deepseek_optimized_prompt_bounds_memory_and_test_work() -> None:
    prompt = harness_headless_task(
        "repository",
        "Fix the bug.",
        agent_preset="deepseek-optimized-offline-v3",
    )

    assert "call it exactly once" in prompt
    assert "Do not call MemoryOS again" in prompt
    assert "one focused verification" in prompt
    assert "make it in the next tool call" in prompt
    assert "no later than the sixth repository-inspection tool call" in prompt
    assert "never announce an edit and then perform another search or read" in prompt
    assert "installed copies of the target project" in prompt
    assert "explicit task contract and current checkout" in prompt
    assert "existing analogous implementation" not in prompt
    assert "Do not run the full repository test suite" in prompt
    assert "memory_explain" not in prompt

    manifest = json.loads(
        (
            ROOT
            / "benchmarks"
            / "real_workload"
            / "swebench_verified"
            / "cross_repo_v1"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    task = next(item for item in manifest["tasks"] if item["id"] == "seaborn-pr-3069")
    v1_prompt = harness_headless_task(
        task["repository_id"],
        task["prompt"],
        agent_preset="deepseek-optimized-offline",
    )
    assert hashlib.sha256(v1_prompt.encode("utf-8")).hexdigest() == (
        "877b1c7bb6a2c2ceb5811c53f0157ced150491fe88d552271427cfecc893d1d0"
    )
    assert v1_prompt != prompt


@pytest.mark.v23
def test_harness_explain_accepts_the_displayed_memory_handle() -> None:
    memory_id = "ee9019a4-9ba7-40f4-883d-188ba0c6b72f"
    atom_sha256 = "6fde4c87a36855f2690f6b98605a043cd35b713512bc22baa036d2980ce970de"
    assert MemoryOSHTTPBridge._explain_arguments(f"{memory_id} @ {atom_sha256}", "") == {
        "memory_id": memory_id,
        "expected_atom_sha256": atom_sha256,
    }
    with pytest.raises(ValueError, match="fingerprint disagree"):
        MemoryOSHTTPBridge._explain_arguments(
            f"{memory_id} @ {atom_sha256}",
            "expected_atom_sha256=" + "0" * 64,
        )


@pytest.mark.v23
@pytest.mark.parametrize(
    ("manifest_path", "task_id", "expected_text"),
    (
        (
            ROOT
            / "benchmarks"
            / "real_workload"
            / "swebench_verified"
            / "requests_6028"
            / "manifest.json",
            "requests-pr-6028",
            "authentication",
        ),
        (
            ROOT / "benchmarks" / "real_workload" / "public_smoke" / "manifest.json",
            "markupsafe-pr-497",
            "deprecationwarning",
        ),
        (
            ROOT
            / "benchmarks"
            / "real_workload"
            / "swebench_verified"
            / "cross_repo_v1"
            / "manifest.json",
            "seaborn-pr-3069",
            "categorical",
        ),
    ),
)
def test_harness_bridge_delivers_compact_task_memory(
    tmp_path: Path,
    manifest_path: Path,
    task_id: str,
    expected_text: str,
) -> None:
    manifest, _runtime = load_context_efficiency_inputs(
        manifest_path,
        ROOT / "runtime" / "deepseek-harness-open-source-ab.json",
    )
    task = next(item for item in manifest.tasks if item.id == task_id)
    seeds = {seed.id: seed for seed in manifest.memories}
    backend = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=ConditionPolicy.for_condition(ContextEfficiencyCondition.MSC_CONTEXT_ONLY),
        task=task.prompt,
        repository=task.repository_id,
        seeds=[seeds[seed_id] for seed_id in task.memory_seed_ids],
        seed_database=True,
        budget_tokens=512,
    )
    bridge = MemoryOSHTTPBridge(
        backend,
        run_id="bridge-preflight",
        task_id=task.id,
        condition=ContextEfficiencyCondition.MSC_CONTEXT_ONLY.value,
        cache_phase=CachePhase.COLD,
    )
    try:
        with bridge:
            body = json.dumps(
                {
                    "task": task.prompt,
                    "repository": task.repository_id,
                    "budget_tokens": 512,
                    "detail_level": "fact",
                    "response_mode": "full",
                }
            ).encode("utf-8")
            request = Request(  # noqa: S310 - URL is the loopback-only test bridge
                bridge.base_url + "/api/context",
                data=body,
                headers={
                    "Authorization": f"Bearer {bridge.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test bridge
                payload = json.load(response)
    finally:
        backend.close()

    assert expected_text in json.dumps(payload, sort_keys=True).lower()
    assert len(bridge.events) == 1
    assert bridge.events[0].ok is True


@pytest.mark.v23
def test_harness_plugin_environment_keeps_usage_but_removes_baseline_tools(
    tmp_path: Path,
) -> None:
    runtime = load_context_efficiency_runtime(ROOT / "runtime" / "deepseek-harness.json")
    assert isinstance(runtime, DeepSeekHarnessRuntime)
    common = {
        "runtime": runtime,
        "usage_path": tmp_path / "provider-usage.jsonl",
        "attempt_path": tmp_path / "provider-attempts.jsonl",
        "task": "same task",
        "repository": "same-repository",
        "run_id": "comparison-run",
        "task_id": "comparison-task",
        "cache_phase": CachePhase.COLD,
        "cache_namespace": "c" * 64,
        "budget_tokens": 6000,
    }

    baseline = _plugin_environment(
        bridge=None,
        condition="no_memory",
        **common,
    )
    treatment = _plugin_environment(
        bridge=cast(
            Any,
            SimpleNamespace(base_url="http://127.0.0.1:12345", token="local-token"),
        ),
        condition="msc_progressive",
        **common,
    )

    assert baseline["MEMORYOS_ENABLED"] == "0"
    assert baseline["MEMORYOS_CONDITION"] == "no_memory"
    assert "MEMORYOS_BASE_URL" not in baseline
    assert "MEMORYOS_AUTH_TOKEN" not in baseline
    assert baseline["MEMORYOS_USAGE_OUTPUT_FILE"] == treatment["MEMORYOS_USAGE_OUTPUT_FILE"]
    assert baseline["MEMORYOS_ATTEMPT_OUTPUT_FILE"] == treatment["MEMORYOS_ATTEMPT_OUTPUT_FILE"]
    assert baseline["MEMORYOS_MODEL"] == treatment["MEMORYOS_MODEL"] == runtime.model
    assert treatment["MEMORYOS_ENABLED"] == "1"
    assert treatment["MEMORYOS_BASE_URL"] == "http://127.0.0.1:12345"
    assert treatment["MEMORYOS_AUTH_TOKEN"] == "local-token"
    assert treatment["MEMORYOS_RESPONSE_FORMAT"] == "deepseek-progressive-compact"

    guard_file = tmp_path / "usage-guard.json"
    guard_file.write_text('{"stop": false}\n', encoding="utf-8")
    guarded = _plugin_environment(
        runtime,
        None,
        condition="no_memory",
        usage_guard_file=guard_file,
        **{key: value for key, value in common.items() if key != "runtime"},
    )
    assert guarded["MEMORYOS_USAGE_GUARD_FILE"] == str(guard_file)

    optimized_runtime = load_context_efficiency_runtime(
        ROOT / "runtime" / "deepseek-harness-optimized.json"
    )
    assert isinstance(optimized_runtime, DeepSeekHarnessRuntime)
    optimized = _plugin_environment(
        bridge=cast(
            Any,
            SimpleNamespace(base_url="http://127.0.0.1:12345", token="local-token"),
        ),
        condition="msc_context_only",
        **{**common, "runtime": optimized_runtime},
    )
    assert optimized["MEMORYOS_BUDGET_TOKENS"] == "512"
    assert optimized["MEMORYOS_MAX_CONTEXT_CALLS"] == "1"
    assert optimized["MEMORYOS_RESPONSE_FORMAT"] == "deepseek-compact"


@pytest.mark.v23
def test_fixture_environment_failure_is_not_labeled_completed(tmp_path: Path) -> None:
    manifest, runtime = load_context_efficiency_inputs(
        ROOT / "benchmarks" / "context_efficiency" / "manifest.json",
        ROOT / "runtime" / "context-efficiency-fixture.json",
    )

    class UnavailableWorkspaceManager:
        @staticmethod
        def prepare_repository(_repository: object) -> None:
            raise OSError("fixture repository unavailable")

    summary = ContextEfficiencyRunner(
        tmp_path / "work",
        workspace_manager=cast(Any, UnavailableWorkspaceManager()),
        hidden_runner=cast(Any, object()),
    ).run(
        manifest,
        runtime,
        hidden_root=tmp_path / "hidden",
        output_root=tmp_path / "output",
        run_id="fixture-environment-blocker",
        config=ContextEfficiencyRunConfig(
            conditions=(ContextEfficiencyCondition.LEGACY_FULL,),
            cache_phases=(CachePhase.COLD,),
        ),
    )

    assert summary["status"] == "external_blocker"
    assert summary["external_blocker_count"] == 1
