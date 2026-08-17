from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from memoryos.context.token_meter import UnicodeHeuristicTokenCounter, canonical_json
from memoryos.domain.schemas import MemoryOperationTokenAttribution
from memoryos.evaluation.context_efficiency import (
    MEMORYOS_CONTEXT_CONDITIONS,
    ContextEfficiencyCondition,
    ContextEfficiencyRecord,
    ProviderTokenAttribution,
    tokenizer_evidence_sha256,
)
from memoryos.evaluation.context_efficiency_report import (
    ContextEfficiencyRunRecord,
    ExecutionStatus,
    StructuredFailure,
    append_jsonl,
    build_context_efficiency_summary,
    render_context_efficiency_summary,
    write_json,
)
from memoryos.evaluation.context_efficiency_runtime import (
    ConditionPolicy,
    MemoryOSToolBackend,
)
from memoryos.evaluation.deepseek_harness_agent import (
    MEMORYOS_PLUGIN_VERSION,
    DeepSeekHarnessCodingAgent,
    DeepSeekHarnessRuntime,
    harness_headless_task,
)
from memoryos.evaluation.openai_compatible_coding_agent import (
    SYSTEM_PROMPT,
    USER_TASK_SUFFIX,
    AgentExternalBlocker,
    AgentRunStatus,
    AgentTransport,
    AllowedTest,
    CodingAgentResult,
    OpenAICompatibleAgentRuntime,
    OpenAICompatibleCodingAgent,
    ToolEvent,
)
from memoryos.evaluation.provider_usage import (
    CachePhase,
    ProviderUsageRecord,
    UsageSource,
    aggregate_usage,
)
from memoryos.evaluation.real_workload_models import (
    DatasetTier,
    ExperimentCondition,
    RealWorkloadManifest,
    RepositorySpec,
    WorkloadTaskSpec,
    load_real_workload_manifest,
)
from memoryos.evaluation.real_workload_scoring import HiddenTestRunner, scan_canary_leakage
from memoryos.evaluation.real_workload_workspace import (
    CapturedPatch,
    MaterializedWorkspace,
    PreparedRepository,
    RepositoryWorkspaceManager,
)

BASELINE_COMMIT = "a1b84b2e0e548733211e0bdb9a97287d1045a74a"
_EMPTY_PATCH_SHA256 = hashlib.sha256(b"").hexdigest()
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_FIXTURE_URI = re.compile(r"^fixture://([a-z0-9][a-z0-9._-]{0,79})$")

ContextEfficiencyRuntime = Annotated[
    OpenAICompatibleAgentRuntime | DeepSeekHarnessRuntime,
    Field(discriminator="adapter"),
]


class ContextEfficiencyRunConfig(BaseModel):
    """Frozen controller inputs that are independent from model decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    conditions: tuple[ContextEfficiencyCondition, ...] = MEMORYOS_CONTEXT_CONDITIONS
    cache_phases: tuple[CachePhase, ...] = (CachePhase.COLD,)
    order_seed: int = Field(default=20260815, ge=0)
    budget_tokens: int = Field(default=6000, ge=256, le=100_000)

    @field_validator("conditions", "cache_phases")
    @classmethod
    def require_unique_nonempty[T](cls, value: tuple[T, ...]) -> tuple[T, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("run selections must be non-empty and unique")
        return value

    @field_validator("cache_phases")
    @classmethod
    def require_cold_before_warm(cls, value: tuple[CachePhase, ...]) -> tuple[CachePhase, ...]:
        if CachePhase.WARM in value and value != (CachePhase.COLD, CachePhase.WARM):
            raise ValueError("warm cache runs require a paired cold-then-warm execution")
        return value

    def digest(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))

    def condition_order(self, task_ordinal: int) -> tuple[ContextEfficiencyCondition, ...]:
        shift = (task_ordinal + self.order_seed) % len(self.conditions)
        return (*self.conditions[shift:], *self.conditions[:shift])


class ContextEfficiencyRunner:
    """Execute isolated coding tasks across controller-selected immutable conditions."""

    def __init__(
        self,
        work_root: Path,
        *,
        workspace_manager: RepositoryWorkspaceManager | None = None,
        hidden_runner: HiddenTestRunner | None = None,
        condition_work_roots: Mapping[ContextEfficiencyCondition, Path] | None = None,
        condition_usage_guard_files: Mapping[ContextEfficiencyCondition, Path] | None = None,
        docker_bind_root_maps: Mapping[Path, Path] | None = None,
    ) -> None:
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        if workspace_manager is not None and condition_work_roots:
            raise ValueError(
                "condition_work_roots cannot be combined with a custom workspace_manager"
            )
        self.workspace_manager = workspace_manager or RepositoryWorkspaceManager(
            self.work_root / "repositories"
        )
        # Scoring is controller-owned.  It must never share the condition-specific
        # filesystem root that is exposed to a coding agent, otherwise a later agent
        # turn can inspect hidden-test material left by an earlier score.
        self.scoring_workspace_manager = self.workspace_manager
        self.condition_workspace_managers: dict[
            ContextEfficiencyCondition, RepositoryWorkspaceManager
        ] = {}
        self.condition_work_roots: dict[ContextEfficiencyCondition, Path] = {}
        for condition, root in (condition_work_roots or {}).items():
            resolved = root.resolve()
            if resolved == self.work_root:
                continue
            resolved.mkdir(parents=True, exist_ok=True)
            self.condition_work_roots[condition] = resolved
            self.condition_workspace_managers[condition] = RepositoryWorkspaceManager(
                resolved / "repositories",
                include_condition_in_workspace_path=False,
            )
            try:
                self.scoring_workspace_manager.root.relative_to(resolved)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "controller scoring repositories must stay outside every condition work root"
                )
        self.condition_usage_guard_files: dict[ContextEfficiencyCondition, Path] = {}
        for condition, guard_file in (condition_usage_guard_files or {}).items():
            condition_root = self.condition_work_roots.get(condition)
            if condition_root is None:
                raise ValueError(
                    f"usage guard for {condition.value} requires a dedicated condition root"
                )
            resolved_guard = guard_file.resolve()
            try:
                resolved_guard.relative_to(condition_root)
            except ValueError as exc:
                raise ValueError(
                    f"usage guard for {condition.value} must stay inside its condition root"
                ) from exc
            self.condition_usage_guard_files[condition] = resolved_guard
        self.hidden_runner = hidden_runner
        self.docker_bind_root_maps = tuple(
            sorted(
                (
                    (source.resolve(strict=True), target.resolve(strict=True))
                    for source, target in (docker_bind_root_maps or {}).items()
                ),
                key=lambda item: len(item[0].parts),
                reverse=True,
            )
        )

    def run(
        self,
        manifest: RealWorkloadManifest,
        runtime: ContextEfficiencyRuntime,
        *,
        hidden_root: Path,
        output_root: Path,
        run_id: str,
        config: ContextEfficiencyRunConfig | None = None,
        task_limit: int | None = None,
        task_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        protocol = config or ContextEfficiencyRunConfig()
        if isinstance(runtime, DeepSeekHarnessRuntime):
            missing_roots = [
                condition.value
                for condition in protocol.conditions
                if condition not in self.condition_work_roots
            ]
            if missing_roots:
                raise ValueError(
                    "DeepSeek Harness conditions require dedicated work roots: "
                    + ", ".join(missing_roots)
                )
            resolved_roots = [self.condition_work_roots[item] for item in protocol.conditions]
            if len(resolved_roots) != len(set(resolved_roots)):
                raise ValueError("DeepSeek Harness condition work roots must be distinct")
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsafe path characters")
        if task_limit is not None and task_limit < 1:
            raise ValueError("task_limit must be positive")
        if task_limit is not None and task_ids is not None:
            raise ValueError("task_limit and task_ids are mutually exclusive")
        if task_ids is not None:
            if not task_ids or len(task_ids) != len(set(task_ids)):
                raise ValueError("task_ids must be non-empty and unique")
            tasks_by_id = {task.id: task for task in manifest.tasks}
            missing = [task_id for task_id in task_ids if task_id not in tasks_by_id]
            if missing:
                raise ValueError(f"unknown task_ids: {', '.join(missing)}")
            tasks = [tasks_by_id[task_id] for task_id in task_ids]
        else:
            tasks = (
                list(manifest.tasks[:task_limit])
                if task_limit is not None
                else list(manifest.tasks)
            )
        output = output_root.resolve()
        if output.exists():
            raise ValueError(f"refusing to reuse output directory: {output}")
        output.mkdir(parents=True)
        for directory in ("patches", "test-results", "failures"):
            (output / directory).mkdir()
        records_path = output / "records.jsonl"
        usage_path = output / "provider-usage.jsonl"
        events_path = output / "tool-events.jsonl"
        for path in (records_path, usage_path, events_path):
            path.touch()

        started_at = datetime.now(UTC)
        runtime_hash = runtime.digest()
        execution_order = [
            {
                "task_id": task.id,
                "condition": condition.value,
                "cache_phase": phase.value,
                "execution_index": execution_index,
            }
            for execution_index, (task, condition, phase) in enumerate(
                (task, condition, phase)
                for task_ordinal, task in enumerate(tasks)
                for condition in protocol.condition_order(task_ordinal)
                for phase in protocol.cache_phases
            )
        ]
        run_manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "study": "context_efficiency",
            "status": "running",
            "run_id": run_id,
            "baseline_commit": BASELINE_COMMIT,
            "started_at": started_at.isoformat(),
            "manifest_sha256": manifest.digest(),
            "runtime_sha256": runtime_hash,
            "protocol": protocol.model_dump(mode="json"),
            "protocol_sha256": protocol.digest(),
            "effective_memory_budget_tokens": (
                runtime.effective_memory_budget_tokens(protocol.budget_tokens)
                if isinstance(runtime, DeepSeekHarnessRuntime)
                else protocol.budget_tokens
            ),
            "condition_policy_sha256": {
                condition.value: ConditionPolicy.for_condition(condition).digest()
                for condition in protocol.conditions
            },
            "runtime": runtime.model_dump(mode="json"),
            "pricing_snapshot_sha256": (
                runtime.pricing.digest() if runtime.pricing is not None else None
            ),
            "execution_order": execution_order,
            "condition_work_roots": {
                condition.value: str(self.condition_work_roots.get(condition, self.work_root))
                for condition in protocol.conditions
            },
            "condition_usage_guard_files": {
                condition.value: str(path)
                for condition, path in self.condition_usage_guard_files.items()
                if condition in protocol.conditions
            },
            "docker_bind_root_maps": {
                str(source): str(target) for source, target in self.docker_bind_root_maps
            },
            "cache_namespace_policy": "sha256(run_id:task_id:condition), same for cold/warm",
            "before_after_design": {
                "baseline": (
                    ContextEfficiencyCondition.NO_MEMORY.value
                    if ContextEfficiencyCondition.NO_MEMORY in protocol.conditions
                    else None
                ),
                "treatments": [
                    condition.value
                    for condition in protocol.conditions
                    if condition is not ContextEfficiencyCondition.NO_MEMORY
                ],
                "controls": (
                    "Same model/runtime, task text, system prompt, starting commit, allowed "
                    "workspace tools, hidden test, and isolated session; only MemoryOS tool "
                    "exposure and returned memory payload differ."
                ),
            },
            "artifacts": [
                "run-manifest.json",
                "records.jsonl",
                "provider-usage.jsonl",
                "tool-events.jsonl",
                "patches/",
                "test-results/",
                "summary.json",
                "summary.md",
            ],
        }
        write_json(output / "run-manifest.json", run_manifest)

        prepared, prepare_failures = self._prepare_repositories(manifest, tasks)
        all_records: list[ContextEfficiencyRunRecord] = []
        all_usage: list[ProviderUsageRecord] = []
        all_events: list[ToolEvent] = []
        execution_index = 0
        seeds = {seed.id: seed for seed in manifest.memories}
        for task_ordinal, task in enumerate(tasks):
            for condition in protocol.condition_order(task_ordinal):
                policy = ConditionPolicy.for_condition(condition)
                cache_namespace = hashlib.sha256(
                    f"{run_id}:{task.id}:{condition.value}".encode()
                ).hexdigest()
                memory_seeded = False
                for phase in protocol.cache_phases:
                    repository_failure = prepare_failures.get(task.repository_id)
                    if repository_failure is not None:
                        artifact = self._blocked_record(
                            manifest,
                            runtime,
                            protocol,
                            task,
                            policy,
                            phase,
                            run_id,
                            execution_index,
                            output,
                            repository_failure,
                        )
                        usage_records: list[ProviderUsageRecord] = []
                        event_records: list[ToolEvent] = []
                    else:
                        artifact, usage_records, event_records, memory_seeded = self._run_one(
                            manifest,
                            runtime,
                            protocol,
                            prepared[task.repository_id],
                            task,
                            policy,
                            phase,
                            run_id,
                            execution_index,
                            cache_namespace,
                            output,
                            hidden_root,
                            [seeds[seed_id] for seed_id in task.memory_seed_ids],
                            memory_seeded,
                        )
                    all_records.append(artifact)
                    all_usage.extend(usage_records)
                    all_events.extend(event_records)
                    append_jsonl(records_path, artifact.model_dump(mode="json"))
                    for request in usage_records:
                        append_jsonl(usage_path, request.model_dump(mode="json"))
                    for event in event_records:
                        append_jsonl(events_path, event.model_dump(mode="json"))
                    execution_index += 1

        evidence_type: Literal["real_coding_agent", "deterministic_fixture"] = (
            "deterministic_fixture" if _is_fixture_runtime(runtime) else "real_coding_agent"
        )
        summary = build_context_efficiency_summary(
            all_records,
            all_usage,
            all_events,
            evidence_type=evidence_type,
        )
        summary.update(
            {
                "run_id": run_id,
                "manifest_sha256": manifest.digest(),
                "runtime_sha256": runtime_hash,
            }
        )
        write_json(output / "summary.json", summary)
        (output / "summary.md").write_text(
            render_context_efficiency_summary(summary), encoding="utf-8", newline="\n"
        )
        finished_at = datetime.now(UTC)
        run_manifest.update(
            {
                "status": summary["status"],
                "finished_at": finished_at.isoformat(),
                "artifact_sha256": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (
                        records_path,
                        usage_path,
                        events_path,
                        output / "summary.json",
                        output / "summary.md",
                    )
                },
            }
        )
        write_json(output / "run-manifest.json", run_manifest)
        return summary

    def _prepare_repositories(
        self,
        manifest: RealWorkloadManifest,
        tasks: list[WorkloadTaskSpec],
    ) -> tuple[dict[str, PreparedRepository], dict[str, StructuredFailure]]:
        prepared: dict[str, PreparedRepository] = {}
        failures: dict[str, StructuredFailure] = {}
        selected_repositories = {task.repository_id for task in tasks}
        for repository in manifest.repositories:
            if repository.id not in selected_repositories:
                continue
            try:
                source = self._fixture_repository(repository, manifest)
                value = self.workspace_manager.prepare_repository(source)
                self.workspace_manager.assert_manifest_commits(
                    value,
                    [task for task in tasks if task.repository_id == repository.id],
                )
                prepared[repository.id] = value
            except Exception as exc:
                failures[repository.id] = StructuredFailure.create(
                    "repository_unavailable",
                    str(exc),
                    exception_type=type(exc).__name__,
                )
        return prepared, failures

    def _fixture_repository(
        self,
        repository: RepositorySpec,
        manifest: RealWorkloadManifest,
    ) -> RepositorySpec:
        match = _FIXTURE_URI.fullmatch(repository.clone_url)
        if match is None:
            return repository
        if manifest.tier is not DatasetTier.HARNESS_FIXTURE:
            raise ValueError("fixture:// repositories are limited to harness_fixture manifests")
        fixture_name = match.group(1)
        project_root = Path(__file__).resolve().parents[2]
        source = project_root / "benchmarks" / "context_efficiency" / "fixtures" / fixture_name
        if not source.is_dir():
            raise ValueError(f"fixture repository source is unavailable: {fixture_name}")
        destination = self.work_root / "fixture-sources" / repository.id
        if destination.exists():
            if not destination.is_dir():
                raise ValueError(f"fixture repository state is not a directory: {repository.id}")
            if _fixture_tree_sha256(source) != _fixture_tree_sha256(destination):
                raise ValueError(
                    "fixture source changed without a fresh work root and manifest lock"
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        git = shutil.which("git")
        if git is None:
            raise ValueError("git is required to build the deterministic fixture repository")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2026-08-15T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-08-15T00:00:00Z",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        if not (destination / ".git").is_dir():
            commands = (
                [git, "init", "--quiet"],
                [git, "config", "core.autocrlf", "false"],
                [git, "config", "core.filemode", "false"],
                [git, "add", "--", "."],
                [
                    git,
                    "-c",
                    "user.name=MemoryOS Fixture",
                    "-c",
                    "user.email=fixture@memoryos.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "deterministic fixture baseline",
                ],
            )
            for command in commands:
                subprocess.run(  # noqa: S603 - fixed git executable and frozen argv
                    command,
                    cwd=destination,
                    env=environment,
                    check=True,
                    capture_output=True,
                )
        resolved = subprocess.run(  # noqa: S603 - fixed git executable and frozen argv
            [git, "rev-parse", "HEAD"],
            cwd=destination,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        clean = subprocess.run(  # noqa: S603 - fixed git executable and frozen argv
            [git, "status", "--porcelain=v1"],
            cwd=destination,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        if clean:
            raise ValueError("fixture repository state is not clean")
        expected = {
            task.base_commit for task in manifest.tasks if task.repository_id == repository.id
        }
        if expected != {resolved}:
            raise ValueError(
                "fixture repository commit does not match the manifest; rebuild its lock"
            )
        return repository.model_copy(update={"clone_url": str(destination)})

    def _run_one(
        self,
        manifest: RealWorkloadManifest,
        runtime: ContextEfficiencyRuntime,
        protocol: ContextEfficiencyRunConfig,
        prepared: PreparedRepository,
        task: WorkloadTaskSpec,
        policy: ConditionPolicy,
        phase: CachePhase,
        run_id: str,
        execution_index: int,
        cache_namespace: str,
        output: Path,
        hidden_root: Path,
        selected_seeds: list[Any],
        memory_seeded: bool,
    ) -> tuple[
        ContextEfficiencyRunRecord,
        list[ProviderUsageRecord],
        list[ToolEvent],
        bool,
    ]:
        started = time.perf_counter()
        key = _artifact_key(task.id, policy.condition.value, phase.value)
        patch_path = output / "patches" / f"{key}.patch"
        test_path = output / "test-results" / f"{key}.json"
        failure_path = output / "failures" / f"{key}.json"
        usage: list[ProviderUsageRecord] = []
        events: list[ToolEvent] = []
        memory: MemoryOSToolBackend | None = None
        agent_result: CodingAgentResult | None = None
        patch = CapturedPatch(patch_path, _EMPTY_PATCH_SHA256, 0, ())
        workspace: MaterializedWorkspace | None = None
        hidden_success = False
        leakage_cross = 0
        leakage_stale = 0
        status = ExecutionStatus.FAILED
        failure: StructuredFailure | None = None
        condition_protocol_error: str | None = None
        test_evidence: dict[str, Any] = {
            "status": "not_run",
            "reason": "agent_did_not_complete",
        }
        effective_budget_tokens = (
            runtime.effective_memory_budget_tokens(protocol.budget_tokens)
            if isinstance(runtime, DeepSeekHarnessRuntime)
            else protocol.budget_tokens
        )
        try:
            workspace_manager = self.condition_workspace_managers.get(
                policy.condition, self.workspace_manager
            )
            workspace = workspace_manager.materialize(
                prepared,
                task,
                cast(ExperimentCondition, policy.condition),
                run_id=f"{run_id}-{phase.value}-{execution_index:05d}",
            )
            if policy.memory_enabled:
                memory_dir = output / ".state" / "memory" / task.id / policy.condition.value
                memory = MemoryOSToolBackend(
                    data_dir=memory_dir,
                    policy=policy,
                    task=task.prompt,
                    repository=task.repository_id,
                    seeds=selected_seeds,
                    seed_database=not memory_seeded,
                    budget_tokens=effective_budget_tokens,
                )
                memory_seeded = True
            if isinstance(runtime, DeepSeekHarnessRuntime):
                condition_root = self.condition_work_roots[policy.condition]
                harness_control = condition_root / ".memoryos-harness" / run_id
                agent_result = DeepSeekHarnessCodingAgent(runtime).run(
                    workspace=workspace.path,
                    memory_tools=memory,
                    state_dir=harness_control / "state" / f"{task.id}__{phase.value}",
                    harness_home=harness_control / "homes" / task.id,
                    filesystem_root=condition_root,
                    task=task.prompt,
                    repository=task.repository_id,
                    run_id=run_id,
                    task_id=task.id,
                    condition=policy.condition.value,
                    cache_phase=phase,
                    cache_namespace=cache_namespace,
                    budget_tokens=effective_budget_tokens,
                    usage_guard_file=self.condition_usage_guard_files.get(policy.condition),
                )
            else:
                agent_result = OpenAICompatibleCodingAgent(runtime).run(
                    workspace=workspace.path,
                    memory_tools=memory,
                    task=task.prompt,
                    repository=task.repository_id,
                    run_id=run_id,
                    task_id=task.id,
                    condition=policy.condition.value,
                    cache_phase=phase,
                    cache_namespace=cache_namespace,
                )
            usage = list(agent_result.usage)
            events = list(agent_result.tool_events)
            patch = workspace_manager.capture_patch(workspace, patch_path)
            if agent_result.status is AgentRunStatus.EXTERNAL_BLOCKER:
                status = ExecutionStatus.EXTERNAL_BLOCKER
                failure = StructuredFailure.create(
                    "external_blocker",
                    agent_result.message or "configured external model is unavailable",
                )
                test_evidence = {
                    "status": "not_run",
                    "reason": "external_blocker",
                }
            else:
                if agent_result.status is AgentRunStatus.COMPLETED:
                    condition_protocol_error = _condition_protocol_error(policy, memory)
                scoring_manager = self.scoring_workspace_manager
                scoring = scoring_manager.materialize(
                    prepared,
                    task,
                    cast(ExperimentCondition, policy.condition),
                    run_id=f"{run_id}-{phase.value}-score-{execution_index:05d}",
                )
                scoring_manager.apply_captured_patch(scoring, patch)
                if manifest.tier is DatasetTier.HARNESS_FIXTURE and _is_fixture_runtime(runtime):
                    test_evidence = _run_local_fixture_test(scoring, task, test_path)
                    hidden_success = bool(test_evidence["success"])
                else:
                    hidden_runner = self.hidden_runner or HiddenTestRunner(
                        scoring_manager,
                        bind_source_resolver=self._docker_bind_source,
                    )
                    hidden = hidden_runner.run(
                        scoring,
                        task.hidden_test,
                        hidden_root=hidden_root,
                        output_dir=output / ".state" / "hidden-tests" / key,
                    )
                    test_evidence = hidden.as_dict()
                    hidden_success = hidden.success
                leakage = scan_canary_leakage(
                    selected_seeds,
                    patch_path=patch.path,
                    text_surfaces={"agent_message": agent_result.message or ""},
                )
                leakage_cross = leakage.cross_project_leaks
                leakage_stale = leakage.stale_memory_uses
                if condition_protocol_error is not None:
                    failure = StructuredFailure.create(
                        "condition_protocol_not_exercised",
                        condition_protocol_error,
                    )
                elif agent_result.status is AgentRunStatus.COMPLETED and hidden_success:
                    status = ExecutionStatus.COMPLETED
                elif agent_result.status is AgentRunStatus.COMPLETED:
                    failure = StructuredFailure.create(
                        "hidden_test_failed", "the frozen hidden test did not pass"
                    )
                else:
                    failure = StructuredFailure.create(
                        agent_result.failure_reason or "agent_failed",
                        agent_result.message or "agent reported failure",
                    )
            write_json(test_path, test_evidence)
        except AgentExternalBlocker as exc:
            status = ExecutionStatus.EXTERNAL_BLOCKER
            failure = StructuredFailure.create(
                "external_blocker", str(exc), exception_type=type(exc).__name__
            )
        except Exception as exc:
            failure = StructuredFailure.create(
                _exception_code(exc), str(exc), exception_type=type(exc).__name__
            )
        finally:
            accounting = memory.accounting_snapshot() if memory is not None else {}
            if memory is not None:
                memory.close()
        if not patch_path.exists():
            patch_path.write_bytes(b"")
        if not test_path.exists():
            write_json(test_path, test_evidence)
        if failure is not None:
            write_json(failure_path, failure.model_dump(mode="json"))
        record = _context_record(
            manifest=manifest,
            runtime=runtime,
            protocol=protocol,
            task=task,
            policy=policy,
            execution_index=execution_index,
            patch=patch,
            workspace=workspace,
            usage=usage,
            events=events,
            accounting=accounting,
            agent_result=agent_result,
            hidden_success=hidden_success,
            execution_valid=(
                status is not ExecutionStatus.EXTERNAL_BLOCKER
                and (agent_result is None or agent_result.failure_reason != "protocol_error")
                and condition_protocol_error is None
                and workspace is not None
            ),
            latency_seconds=round(time.perf_counter() - started, 6),
            leakage_cross=leakage_cross,
            leakage_stale=leakage_stale,
        )
        artifact = ContextEfficiencyRunRecord(
            run_id=run_id,
            cache_phase=phase.value,
            status=status,
            record=record,
            failure=failure,
            agent_steps=agent_result.steps if agent_result is not None else 0,
            provider_requests=len(usage),
            provider_attempts=(agent_result.provider_attempts if agent_result is not None else 0),
            tests_run=agent_result.tests_run if agent_result is not None else 0,
            patches_applied=agent_result.patches_applied if agent_result is not None else 0,
            patch_path=_relative_artifact(output, patch_path),
            test_result_path=_relative_artifact(output, test_path),
            failure_path=(
                _relative_artifact(output, failure_path) if failure is not None else None
            ),
        )
        return artifact, usage, events, memory_seeded

    def _docker_bind_source(self, path: Path) -> Path:
        source = path.resolve(strict=True)
        for container_root, docker_host_root in self.docker_bind_root_maps:
            try:
                relative = source.relative_to(container_root)
            except ValueError:
                continue
            mapped = (docker_host_root / relative).resolve(strict=True)
            return mapped
        return source

    def _blocked_record(
        self,
        manifest: RealWorkloadManifest,
        runtime: ContextEfficiencyRuntime,
        protocol: ContextEfficiencyRunConfig,
        task: WorkloadTaskSpec,
        policy: ConditionPolicy,
        phase: CachePhase,
        run_id: str,
        execution_index: int,
        output: Path,
        failure: StructuredFailure,
    ) -> ContextEfficiencyRunRecord:
        key = _artifact_key(task.id, policy.condition.value, phase.value)
        patch_path = output / "patches" / f"{key}.patch"
        test_path = output / "test-results" / f"{key}.json"
        failure_path = output / "failures" / f"{key}.json"
        patch_path.write_bytes(b"")
        write_json(test_path, {"status": "not_run", "reason": failure.code})
        write_json(failure_path, failure.model_dump(mode="json"))
        record = _context_record(
            manifest=manifest,
            runtime=runtime,
            protocol=protocol,
            task=task,
            policy=policy,
            execution_index=execution_index,
            patch=CapturedPatch(patch_path, _EMPTY_PATCH_SHA256, 0, ()),
            workspace=None,
            usage=[],
            events=[],
            accounting={},
            agent_result=None,
            hidden_success=False,
            execution_valid=False,
            latency_seconds=0,
            leakage_cross=0,
            leakage_stale=0,
        )
        return ContextEfficiencyRunRecord(
            run_id=run_id,
            cache_phase=phase.value,
            status=ExecutionStatus.EXTERNAL_BLOCKER,
            record=record,
            failure=failure,
            patch_path=_relative_artifact(output, patch_path),
            test_result_path=_relative_artifact(output, test_path),
            failure_path=_relative_artifact(output, failure_path),
        )


def _context_record(
    *,
    manifest: RealWorkloadManifest,
    runtime: ContextEfficiencyRuntime,
    protocol: ContextEfficiencyRunConfig,
    task: WorkloadTaskSpec,
    policy: ConditionPolicy,
    execution_index: int,
    patch: CapturedPatch,
    workspace: MaterializedWorkspace | None,
    usage: list[ProviderUsageRecord],
    events: list[ToolEvent],
    accounting: Mapping[str, int | None],
    agent_result: CodingAgentResult | None,
    hidden_success: bool,
    execution_valid: bool,
    latency_seconds: float,
    leakage_cross: int,
    leakage_stale: int,
) -> ContextEfficiencyRecord:
    totals = aggregate_usage(usage)
    attributed = bool(usage) and all(
        item.usage_source in {UsageSource.PROVIDER_EXACT, UsageSource.TOKENIZER_EXACT}
        for item in usage
    )
    provider_attribution = (
        ProviderTokenAttribution.EXACT if attributed else ProviderTokenAttribution.UNAVAILABLE
    )
    schemas = {
        "policy": policy.model_dump(mode="json"),
        "allowed_tests": [item.model_dump(mode="json") for item in _runtime_allowed_tests(runtime)],
        "memory_schema_sha256": sorted(
            {
                hashlib.sha256(
                    canonical_json(
                        {
                            "memory": item.memory_tool_schema_tokens,
                            "other": item.other_tool_schema_tokens,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                for item in usage
            }
        ),
    }
    counter = UnicodeHeuristicTokenCounter()
    provider_input = totals.input_tokens if attributed else None
    provider_output = totals.output_tokens if attributed else None
    cached = totals.cache_hit_tokens if attributed else None
    sequence_size = sum(item.sequence_id == task.sequence_id for item in manifest.tasks)
    memory_events = [item for item in events if item.category == "memory"]
    searches = sum(item.tool == "search_files" for item in events)
    opens = sum(item.tool == "read_file" for item in events)
    memory_schema_tokens = next(
        (
            item.memory_tool_schema_tokens
            for item in usage
            if item.memory_tool_schema_tokens is not None
        ),
        None,
    )
    other_schema_tokens = next(
        (
            item.other_tool_schema_tokens
            for item in usage
            if item.other_tool_schema_tokens is not None
        ),
        None,
    )
    return ContextEfficiencyRecord(
        task_id=task.id,
        repository_id=task.repository_id,
        sequence_id=task.sequence_id,
        sequence_index=task.sequence_index,
        is_cross_step=sequence_size > 1,
        intent=task.prompt,
        agent_version=_runtime_agent_version(runtime),
        model=f"{runtime.provider}:{runtime.model}@{runtime.model_revision}",
        image_digest=_canonical_sha256(
            {
                "adapter": runtime.adapter,
                "model": runtime.model,
                "revision": runtime.model_revision,
                "quantization": runtime.quantization,
            }
        ),
        runtime_sha256=runtime.digest(),
        evidence_type=(
            "deterministic_fixture" if _is_fixture_runtime(runtime) else "real_coding_agent"
        ),
        dataset_tier=_dataset_tier(manifest.tier),
        condition=policy.condition,
        execution_index=execution_index,
        prompt_sha256=_prompt_sha256(task, runtime),
        starting_state_sha256=hashlib.sha256(task.base_commit.encode("ascii")).hexdigest(),
        patch_sha256=patch.sha256,
        study_config_sha256=protocol.digest(),
        policy_sha256=policy.digest(),
        tool_profile=policy.tool_profile.value,
        tool_schema_sha256=_canonical_sha256(schemas),
        dataset_sha256=manifest.digest(),
        tokenizer_id=counter.tokenizer_id,
        counter_kind=counter.kind.value,
        counter_version=counter.counter_version,
        tokenizer_sha256=tokenizer_evidence_sha256(
            tokenizer_id=counter.tokenizer_id,
            counter_kind=counter.kind.value,
            counter_version=counter.counter_version,
        ),
        provider_token_attribution=provider_attribution,
        provider_input_tokens=provider_input,
        provider_output_tokens=provider_output,
        cached_input_tokens=cached,
        cost_usd=(float(totals.cost_usd) if attributed and totals.cost_usd is not None else None),
        latency_seconds=latency_seconds,
        agent_completed=(
            agent_result is not None and agent_result.status is AgentRunStatus.COMPLETED
        ),
        hidden_test_success=hidden_success,
        execution_valid=execution_valid,
        memory_context_text_tokens=_memory_metric(policy, accounting, "context_text_tokens"),
        memory_delivery_payload_tokens=_memory_metric(
            policy, accounting, "delivered_payload_tokens"
        ),
        memory_payload_overhead_tokens=_memory_metric(
            policy, accounting, "payload_overhead_tokens"
        ),
        memory_evidence_tokens=_memory_metric(policy, accounting, "evidence_expansion_tokens"),
        memory_history_tokens=_memory_metric(policy, accounting, "history_expansion_tokens"),
        memory_delta_tokens=_memory_metric(policy, accounting, "delta_tokens"),
        memory_full_equivalent_tokens=_memory_metric(policy, accounting, "full_context_tokens"),
        context_compilation_llm_input_tokens=0,
        context_compilation_llm_output_tokens=0,
        other_memory_operation_llm_input_tokens=0,
        other_memory_operation_llm_output_tokens=0,
        other_memory_operation_token_attribution=MemoryOperationTokenAttribution.EXACT_ZERO,
        memory_tool_schema_tokens=memory_schema_tokens,
        other_tool_schema_tokens=other_schema_tokens,
        memory_explain_calls=sum(item.tool == "memory_explain" and item.ok for item in events),
        memory_tool_calls=len(memory_events),
        stale_memory_uses=leakage_stale,
        cross_project_leaks=leakage_cross,
        repeated_searches=max(0, searches - 1),
        repeated_file_opens=max(0, opens - 1),
        blocked_actions=sum(item.blocked for item in events)
        + int(accounting.get("memory_blocked_actions") or 0),
        context_rebases=int(accounting.get("context_rebases") or 0),
        delta_hits=int(accounting.get("delta_hits") or 0),
        full_fallbacks=int(accounting.get("full_fallbacks") or 0),
        snapshot_misses=int(accounting.get("snapshot_misses") or 0),
    )


def _run_local_fixture_test(
    workspace: MaterializedWorkspace,
    task: WorkloadTaskSpec,
    output_path: Path,
) -> dict[str, Any]:
    spec = task.hidden_test
    if spec.hidden_patch is not None:
        raise ValueError("local fixture scoring does not accept hidden patches")
    command = list(spec.command)
    if command[0] in {"python", "python3"}:
        command[0] = sys.executable
    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        )
        if name in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    result = subprocess.run(  # noqa: S603 - frozen fixture manifest and direct argv
        command,
        cwd=workspace.path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=spec.timeout_seconds,
        check=False,
    )
    evidence = {
        "status": "completed",
        "success": result.returncode == spec.expected_exit_code,
        "expected_exit_code": spec.expected_exit_code,
        "actual_exit_code": result.returncode,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "command_sha256": _canonical_sha256(spec.command),
        "stdout": result.stdout[-32_000:],
        "stderr": result.stderr[-32_000:],
        "fixture_only": True,
    }
    write_json(output_path, evidence)
    return evidence


def load_context_efficiency_runtime(path: Path) -> ContextEfficiencyRuntime:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid context efficiency runtime JSON: {path}") from exc
    return TypeAdapter(ContextEfficiencyRuntime).validate_python(payload)


def load_context_efficiency_inputs(
    manifest_path: Path,
    runtime_path: Path,
) -> tuple[RealWorkloadManifest, ContextEfficiencyRuntime]:
    return load_real_workload_manifest(manifest_path), load_context_efficiency_runtime(runtime_path)


def _is_fixture_runtime(runtime: ContextEfficiencyRuntime) -> bool:
    return (
        isinstance(runtime, OpenAICompatibleAgentRuntime)
        and runtime.transport is AgentTransport.FIXTURE
    )


def _runtime_allowed_tests(runtime: ContextEfficiencyRuntime) -> tuple[AllowedTest, ...]:
    return runtime.allowed_tests if isinstance(runtime, OpenAICompatibleAgentRuntime) else ()


def _runtime_agent_version(runtime: ContextEfficiencyRuntime) -> str:
    if isinstance(runtime, DeepSeekHarnessRuntime):
        return (
            f"deepseek-harness/{runtime.harness_version}"
            f"@{runtime.harness_commit}+memoryos-plugin/{MEMORYOS_PLUGIN_VERSION}"
        )
    return "memoryos-openai-compatible-coding-agent-v3"


def _prompt_sha256(task: WorkloadTaskSpec, runtime: ContextEfficiencyRuntime) -> str:
    if isinstance(runtime, DeepSeekHarnessRuntime):
        return hashlib.sha256(
            harness_headless_task(
                task.repository_id,
                task.prompt,
                agent_preset=runtime.agent_preset,
            ).encode("utf-8")
        ).hexdigest()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Repository: {task.repository_id}\n\nTask:\n{task.prompt.strip()}\n\n"
                + USER_TASK_SUFFIX
            ),
        },
    ]
    return hashlib.sha256(canonical_json(messages).encode("utf-8")).hexdigest()


def _dataset_tier(value: DatasetTier) -> Literal["public_replay", "private_authorized", "fixture"]:
    if value is DatasetTier.PUBLIC_REPLAY:
        return "public_replay"
    if value is DatasetTier.PRIVATE_OPT_IN:
        return "private_authorized"
    return "fixture"


def _fixture_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("fixture source cannot contain symbolic links")
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _artifact_key(task_id: str, condition: str, phase: str) -> str:
    return f"{task_id}__{condition}__{phase}"


def _relative_artifact(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _exception_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"runner_{name}"


def _condition_protocol_error(
    policy: ConditionPolicy,
    memory: MemoryOSToolBackend | None,
) -> str | None:
    if not policy.memory_enabled:
        if memory is not None:
            return "the no-memory condition instantiated MemoryOS"
        return None
    if memory is None:
        return "the memory-enabled condition did not instantiate MemoryOS"
    if memory.context_calls == 0:
        return "the coding agent completed without calling memory_context"
    if policy.use_previous_context and memory.context_calls < 2:
        return "the delta condition completed without a second memory_context checkpoint"
    return None


def _memory_metric(
    policy: ConditionPolicy,
    accounting: Mapping[str, int | None],
    key: str,
) -> int | None:
    if not policy.memory_enabled:
        return 0
    return accounting.get(key)


__all__ = [
    "BASELINE_COMMIT",
    "ContextEfficiencyRunConfig",
    "ContextEfficiencyRunner",
    "load_context_efficiency_inputs",
    "load_context_efficiency_runtime",
]
