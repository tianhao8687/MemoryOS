from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from memoryos.evaluation.real_workload_agent import (
    AgentEvidenceType,
    AgentExecutionError,
    AgentRuntimeSpec,
    DockerAgentExecutor,
)
from memoryos.evaluation.real_workload_containers import (
    ContainerCommandResult,
    ContainerRuntimeError,
    prepare_writable_bind_mount,
)
from memoryos.evaluation.real_workload_memory import MemoryRuntimeBuilder
from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    MemorySeedSpec,
    WorkloadTaskSpec,
)
from memoryos.evaluation.real_workload_workspace import MaterializedWorkspace

IMAGE = "benchmark-agent@sha256:" + "a" * 64
MCP_IMAGE = "benchmark-mcp@sha256:" + "b" * 64
HIDDEN_IMAGE = "python@sha256:" + "c" * 64


class FakeContainerEngine:
    def __init__(self, *, write_result: bool = True) -> None:
        self.write_result = write_result
        self.networks: list[tuple[str, bool]] = []
        self.removed_networks: list[str] = []
        self.detached_arguments: list[list[str]] = []
        self.attached_arguments: list[list[str]] = []
        self.removed_containers: list[str] = []
        self.probes: list[tuple[str, str, str, int]] = []

    def create_network(self, name: str, *, internal: bool) -> None:
        self.networks.append((name, internal))

    def remove_network(self, name: str) -> None:
        self.removed_networks.append(name)

    def remove_container(self, name: str) -> None:
        self.removed_containers.append(name)

    def start_detached(self, arguments: list[str]) -> str:
        self.detached_arguments.append(arguments)
        return "container-id"

    def probe_python_socket(
        self,
        container: str,
        *,
        python_command: str,
        host: str,
        port: int,
        timeout_seconds: float = 30,
    ) -> None:
        del timeout_seconds
        self.probes.append((container, python_command, host, port))

    def run_attached(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        max_log_bytes: int,
    ) -> ContainerCommandResult:
        del timeout_seconds, max_log_bytes
        self.attached_arguments.append(arguments)
        stdout_path.write_text("agent output\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        if self.write_result:
            result_file = _mount_source(arguments, "/output/agent-result.json")
            result_file.write_text(
                '{"status":"completed","input_tokens":100,"output_tokens":20,'
                '"cost_usd":0.01,"tool_calls":4}\n',
                encoding="utf-8",
            )
        return ContainerCommandResult(
            exit_code=0,
            duration_seconds=1.25,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
        )


def _mount_source(arguments: list[str], destination: str) -> Path:
    for index, argument in enumerate(arguments[:-1]):
        if argument != "--mount":
            continue
        fields = dict(
            field.split("=", maxsplit=1)
            for field in arguments[index + 1].split(",")
            if "=" in field
        )
        if fields.get("dst") == destination:
            return Path(fields["src"])
    raise AssertionError(f"mount not found: {destination}")


def _runtime_spec() -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        image=IMAGE,
        mcp_image=MCP_IMAGE,
        command=[
            "agent-cli",
            "--workspace",
            "{workspace}",
            "--prompt",
            "{prompt_file}",
            "--mcp-config",
            "{mcp_config}",
            "--result",
            "{result_file}",
        ],
        provider="fixture",
        model="deterministic-agent",
        agent_version="1.0",
        evidence_type=AgentEvidenceType.DETERMINISTIC_FIXTURE,
    )


def _task() -> WorkloadTaskSpec:
    return WorkloadTaskSpec.model_validate(
        {
            "id": "agent-task",
            "repository_id": "project",
            "sequence_id": "agent-sequence",
            "sequence_index": 1,
            "base_commit": "1" * 40,
            "cutoff": "2025-02-01T00:00:00Z",
            "prompt": "Implement the requested parser change.",
            "memory_seed_ids": ["decision"],
            "hidden_test": {
                "image": HIDDEN_IMAGE,
                "command": ["python", "-m", "pytest"],
            },
        }
    )


def _seed() -> MemorySeedSpec:
    return MemorySeedSpec.model_validate(
        {
            "id": "decision",
            "repository_id": "project",
            "category": "decision",
            "title": "Parser Result",
            "content": "Return an explicit Result object.",
            "captured_at": "2025-01-01T00:00:00Z",
            "source_ref": "docs/decision.md",
        }
    )


def _workspace(tmp_path: Path) -> MaterializedWorkspace:
    path = tmp_path / "workspace"
    path.mkdir()
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return MaterializedWorkspace(
        repository_id="project",
        task_id="agent-task",
        condition=ExperimentCondition.FLAT_MEMORY,
        path=path,
        base_commit="1" * 40,
        git_control_sha256="f" * 64,
    )


def test_executor_uses_sidecar_and_hardened_agent_mounts(tmp_path: Path) -> None:
    task = _task()
    state = tmp_path / "state"
    runtime = MemoryRuntimeBuilder().prepare(
        ExperimentCondition.FLAT_MEMORY,
        task,
        [_seed()],
        state,
        path_mapper=lambda path: f"/state/{path.relative_to(state).as_posix()}",
        http_url="http://benchmark-memory:8000/mcp",
    )
    prompt = tmp_path / "task.txt"
    prompt.write_text(task.prompt, encoding="utf-8")
    engine = FakeContainerEngine()

    evidence = DockerAgentExecutor(engine).run(
        _runtime_spec(),
        _workspace(tmp_path),
        runtime,
        prompt,
        tmp_path / "output",
    )

    assert evidence.result.status == "completed"
    assert evidence.result.input_tokens == 100
    assert evidence.prompt_sha256
    assert engine.networks[0][1] is True
    assert len(engine.detached_arguments) == 1
    sidecar = engine.detached_arguments[0]
    assert "--read-only" in sidecar
    assert "--cap-drop" in sidecar
    assert "--network-alias" in sidecar
    assert "benchmark-memory" in sidecar
    assert "--publish" not in sidecar
    assert len(engine.probes) == 1

    agent = engine.attached_arguments[0]
    assert "--read-only" in agent
    assert "no-new-privileges" in agent
    assert agent.count("--mount") == 4
    assert _mount_source(agent, "/output/agent-result.json").is_file()
    assert "/output" not in [
        fields.get("dst")
        for index, argument in enumerate(agent[:-1])
        if argument == "--mount"
        for fields in [
            dict(
                field.split("=", maxsplit=1)
                for field in agent[index + 1].split(",")
                if "=" in field
            )
        ]
    ]
    assert "/state" not in agent
    assert str(state / "flat-seeds.json") not in agent
    assert "flat_memory" not in agent
    assert engine.removed_networks == [engine.networks[0][0]]
    assert len(engine.removed_containers) == 2


def test_executor_rejects_missing_structured_result(tmp_path: Path) -> None:
    task = _task()
    runtime = MemoryRuntimeBuilder().prepare(
        ExperimentCondition.NO_MEMORY,
        task,
        [_seed()],
        tmp_path / "baseline-state",
    )
    prompt = tmp_path / "task.txt"
    prompt.write_text(task.prompt, encoding="utf-8")

    with pytest.raises(AgentExecutionError, match="structured result"):
        DockerAgentExecutor(FakeContainerEngine(write_result=False)).run(
            _runtime_spec(),
            _workspace(tmp_path),
            runtime,
            prompt,
            tmp_path / "missing-output",
        )


def test_runtime_loading_is_pure_but_executor_rejects_missing_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variable = "MEMORYOS_TEST_MISSING_AGENT_TOKEN"
    monkeypatch.delenv(variable, raising=False)
    payload = _runtime_spec().model_dump(mode="json")
    payload["environment_variables"] = [variable]
    spec = AgentRuntimeSpec.model_validate(payload)
    task = _task()
    runtime = MemoryRuntimeBuilder().prepare(
        ExperimentCondition.NO_MEMORY,
        task,
        [_seed()],
        tmp_path / "missing-env-state",
    )
    prompt = tmp_path / "missing-env-task.txt"
    prompt.write_text(task.prompt, encoding="utf-8")
    engine = FakeContainerEngine()

    with pytest.raises(AgentExecutionError, match=variable):
        DockerAgentExecutor(engine).run(
            spec,
            _workspace(tmp_path),
            runtime,
            prompt,
            tmp_path / "missing-env-output",
        )

    assert engine.networks == []


def test_writable_bind_mount_rejects_nonroot_uid_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bind-file"
    target.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        "memoryos.evaluation.real_workload_containers.os.getuid",
        lambda: 1000,
        raising=False,
    )
    monkeypatch.setattr(
        "memoryos.evaluation.real_workload_containers.os.chown",
        lambda *_args, **_kwargs: None,
        raising=False,
    )

    with pytest.raises(ContainerRuntimeError, match="must match non-root host uid"):
        prepare_writable_bind_mount(target, "1001:1001", recursive=False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"image": "benchmark-agent:latest"}, "pinned by sha256"),
        ({"user": "0:0"}, "must not run as root"),
        (
            {
                "command": [
                    "sh",
                    "-c",
                    "agent {workspace} {prompt_file} {mcp_config} {result_file}",
                ]
            },
            "direct argv",
        ),
        (
            {"command": ["agent", "{workspace}", "{prompt_file}", "{result_file}"]},
            "missing placeholders",
        ),
    ],
)
def test_runtime_spec_rejects_unreproducible_or_unsafe_execution(
    change: dict[str, Any], message: str
) -> None:
    payload = _runtime_spec().model_dump()
    payload.update(change)
    with pytest.raises(ValidationError, match=message):
        AgentRuntimeSpec.model_validate(payload)
