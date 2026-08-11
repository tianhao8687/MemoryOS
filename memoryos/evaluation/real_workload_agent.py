from __future__ import annotations

import hashlib
import json
import os
import re
import string
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memoryos.evaluation.real_workload_containers import (
    ContainerCommandResult,
    DockerEngine,
    bind_mount,
    default_container_user,
    make_bind_mount_world_readable,
    prepare_writable_bind_mount,
)
from memoryos.evaluation.real_workload_memory import MemoryRuntime
from memoryos.evaluation.real_workload_models import ExperimentCondition
from memoryos.evaluation.real_workload_workspace import MaterializedWorkspace

_PINNED_IMAGE = re.compile(
    r"(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[0-9a-f]{64}$",
    flags=re.IGNORECASE,
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_CONTAINER_USER = re.compile(r"^[1-9][0-9]{0,9}:[0-9]{1,10}$")
_REQUIRED_PLACEHOLDERS = {"workspace", "prompt_file", "mcp_config", "result_file"}
_ALLOWED_PLACEHOLDERS = _REQUIRED_PLACEHOLDERS
_FORBIDDEN_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
}
_MCP_TMPFS = "/tmp:rw,noexec,nosuid,size=128m"  # noqa: S108 - container-only tmpfs
_AGENT_TMPFS = "/tmp:rw,noexec,nosuid,size=512m"  # noqa: S108 - container-only tmpfs
_CONTAINER_BIND = "0.0.0.0"  # noqa: S104 - unexposed, isolated Docker network only
_CREDENTIAL_ROOT = PurePosixPath("/run/credentials")
_MAX_CREDENTIAL_BYTES = 2 * 1024 * 1024


class NetworkAccess(StrEnum):
    INTERNAL = "internal"
    INTERNET = "internet"


class AgentEvidenceType(StrEnum):
    DETERMINISTIC_FIXTURE = "deterministic_fixture"
    REAL_CODING_AGENT = "real_coding_agent"


class CredentialMountSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_environment: str
    destination: str

    @field_validator("source_environment")
    @classmethod
    def validate_source_environment(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("credential source must be a portable environment variable name")
        return value

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("credential destination must be an absolute container path")
        try:
            relative = path.relative_to(_CREDENTIAL_ROOT)
        except ValueError as exc:
            raise ValueError("credential destination must be under /run/credentials") from exc
        if not relative.parts:
            raise ValueError("credential destination must name a file under /run/credentials")
        return path.as_posix()


class AgentRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    mcp_image: str
    command: list[str] = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=300)
    agent_version: str = Field(min_length=1, max_length=120)
    evidence_type: AgentEvidenceType
    environment_variables: list[str] = Field(default_factory=list, max_length=100)
    credential_mounts: list[CredentialMountSpec] = Field(default_factory=list, max_length=10)
    network_access: NetworkAccess = NetworkAccess.INTERNAL
    allow_unconfined_seccomp_for_nested_sandbox: bool = False
    user: str = Field(default_factory=default_container_user)
    mcp_user: str = Field(default_factory=default_container_user)
    scoring_user: str = Field(default_factory=default_container_user)
    mcp_python_command: str = "python"
    timeout_seconds: int = Field(default=900, ge=1, le=7200)
    memory_mb: int = Field(default=4096, ge=256, le=32_768)
    cpus: float = Field(default=2.0, gt=0, le=16)
    pids_limit: int = Field(default=512, ge=32, le=4096)
    max_log_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

    @field_validator("image", "mcp_image")
    @classmethod
    def require_pinned_image(cls, value: str) -> str:
        if not _PINNED_IMAGE.fullmatch(value):
            raise ValueError("container images must be pinned by sha256")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("agent command must contain non-empty argv without NUL bytes")
        executable = PurePosixPath(value[0].replace("\\", "/")).name.lower()
        if executable in _FORBIDDEN_EXECUTABLES:
            raise ValueError("agent command must use direct argv execution, not a shell")
        formatter = string.Formatter()
        placeholders = {
            field_name
            for argument in value
            for _, field_name, _, _ in formatter.parse(argument)
            if field_name is not None
        }
        unknown = placeholders - _ALLOWED_PLACEHOLDERS
        if unknown:
            raise ValueError(f"unsupported agent command placeholders: {sorted(unknown)}")
        missing = _REQUIRED_PLACEHOLDERS - placeholders
        if missing:
            raise ValueError(f"agent command is missing placeholders: {sorted(missing)}")
        return value

    @field_validator("environment_variables")
    @classmethod
    def validate_environment_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not _ENV_NAME.fullmatch(item) for item in value):
            raise ValueError("environment variable names must be unique portable identifiers")
        return value

    @model_validator(mode="after")
    def validate_credential_mounts(self) -> AgentRuntimeSpec:
        sources = [item.source_environment for item in self.credential_mounts]
        destinations = [item.destination for item in self.credential_mounts]
        if len(set(sources)) != len(sources):
            raise ValueError("credential source environment variables must be unique")
        if len(set(destinations)) != len(destinations):
            raise ValueError("credential destinations must be unique")
        overlap = sorted(set(sources) & set(self.environment_variables))
        if overlap:
            raise ValueError(
                "credential path variables must not be injected into the agent environment: "
                + ", ".join(overlap)
            )
        if (
            self.allow_unconfined_seccomp_for_nested_sandbox
            and self.evidence_type is not AgentEvidenceType.REAL_CODING_AGENT
        ):
            raise ValueError(
                "unconfined seccomp is reserved for real agents with their own nested sandbox"
            )
        return self

    @field_validator("user", "mcp_user", "scoring_user")
    @classmethod
    def reject_root_user(cls, value: str) -> str:
        if value.lower() in {"0", "0:0", "root", "root:root"} or value.startswith("0:"):
            raise ValueError("benchmark containers must not run as root")
        if not _CONTAINER_USER.fullmatch(value):
            raise ValueError("benchmark container users must be numeric uid:gid pairs")
        return value


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed", "failed"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    message: str | None = Field(default=None, max_length=2000)


@dataclass(frozen=True)
class AgentExecutionEvidence:
    provider: str
    model: str
    agent_version: str
    image: str
    prompt_sha256: str
    result: AgentOutput
    container: ContainerCommandResult

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["result"] = self.result.model_dump(mode="json")
        payload["container"]["stdout_path"] = str(self.container.stdout_path)
        payload["container"]["stderr_path"] = str(self.container.stderr_path)
        return payload


class AgentExecutionError(RuntimeError):
    """Raised when a coding-agent container does not produce valid benchmark evidence."""


class ContainerEngine(Protocol):
    def create_network(self, name: str, *, internal: bool) -> None: ...

    def remove_network(self, name: str) -> None: ...

    def remove_container(self, name: str) -> None: ...

    def start_detached(self, arguments: list[str]) -> str: ...

    def probe_python_socket(
        self,
        container: str,
        *,
        python_command: str,
        host: str,
        port: int,
        timeout_seconds: float = 30,
    ) -> None: ...

    def run_attached(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        max_log_bytes: int,
    ) -> ContainerCommandResult: ...


class DockerAgentExecutor:
    def __init__(self, engine: ContainerEngine | None = None) -> None:
        self.engine = engine or DockerEngine()

    def run(
        self,
        spec: AgentRuntimeSpec,
        workspace: MaterializedWorkspace,
        memory: MemoryRuntime,
        prompt_path: Path,
        output_dir: Path,
    ) -> AgentExecutionEvidence:
        required_environment = [
            *spec.environment_variables,
            *(item.source_environment for item in spec.credential_mounts),
        ]
        missing_environment = [name for name in required_environment if name not in os.environ]
        if missing_environment:
            raise AgentExecutionError(
                "required host environment variables are missing: " + ", ".join(missing_environment)
            )
        prompt = prompt_path.resolve(strict=True)
        config = memory.config_path.resolve(strict=True)
        credential_mounts = self._resolve_credential_mounts(spec, workspace)
        output = output_dir.resolve()
        try:
            output.relative_to(workspace.path.resolve())
        except ValueError:
            pass
        else:
            raise AgentExecutionError("agent output directory must be outside the workspace")
        if output.exists():
            raise AgentExecutionError(f"refusing to reuse agent output directory: {output}")
        output.mkdir(parents=True)
        result_path = output / "agent-result.json"
        result_path.touch()
        log_directory = output / "harness-logs"
        log_directory.mkdir()
        prepare_writable_bind_mount(workspace.path, spec.user, recursive=True)
        prepare_writable_bind_mount(result_path, spec.user, recursive=False)
        make_bind_mount_world_readable(prompt)
        if memory.condition is not ExperimentCondition.NO_MEMORY:
            prepare_writable_bind_mount(config.parent, spec.mcp_user, recursive=True)
        make_bind_mount_world_readable(config)
        suffix = uuid.uuid4().hex[:12]
        network_name = f"memoryos-bench-{suffix}"
        agent_name = f"memoryos-agent-{suffix}"
        mcp_name = f"memoryos-mcp-{suffix}"
        self.engine.create_network(
            network_name,
            internal=spec.network_access is NetworkAccess.INTERNAL,
        )
        try:
            if memory.condition is not ExperimentCondition.NO_MEMORY:
                self._start_memory_server(spec, memory, network_name, mcp_name)
            arguments = self._agent_arguments(
                spec,
                workspace,
                prompt,
                config,
                result_path,
                network_name,
                agent_name,
                credential_mounts,
            )
            container = self.engine.run_attached(
                arguments,
                timeout_seconds=spec.timeout_seconds,
                stdout_path=log_directory / "agent.stdout.log",
                stderr_path=log_directory / "agent.stderr.log",
                max_log_bytes=spec.max_log_bytes,
            )
        finally:
            self.engine.remove_container(agent_name)
            self.engine.remove_container(mcp_name)
            self.engine.remove_network(network_name)
        if container.timed_out:
            raise AgentExecutionError("agent container timed out")
        if container.exit_code != 0:
            raise AgentExecutionError(f"agent container exited with {container.exit_code}")
        result = _load_agent_output(result_path)
        return AgentExecutionEvidence(
            provider=spec.provider,
            model=spec.model,
            agent_version=spec.agent_version,
            image=spec.image,
            prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
            result=result,
            container=container,
        )

    def _start_memory_server(
        self,
        spec: AgentRuntimeSpec,
        memory: MemoryRuntime,
        network_name: str,
        container_name: str,
    ) -> None:
        if not memory.server_arguments:
            raise AgentExecutionError("memory runtime has no sidecar server arguments")
        state = memory.config_path.parent.resolve(strict=True)
        arguments = [
            "--name",
            container_name,
            "--network",
            network_name,
            "--network-alias",
            "benchmark-memory",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1024m",
            "--cpus",
            "1",
            "--user",
            spec.mcp_user,
            "--tmpfs",
            _MCP_TMPFS,
            "--mount",
            bind_mount(state, "/state", read_only=False),
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            spec.mcp_image,
            spec.mcp_python_command,
            *memory.server_arguments,
            "--transport",
            "streamable-http",
            "--host",
            _CONTAINER_BIND,
            "--port",
            "8000",
        ]
        self.engine.start_detached(arguments)
        self.engine.probe_python_socket(
            container_name,
            python_command=spec.mcp_python_command,
            host="127.0.0.1",
            port=8000,
        )

    @staticmethod
    def _agent_arguments(
        spec: AgentRuntimeSpec,
        workspace: MaterializedWorkspace,
        prompt: Path,
        config: Path,
        result_path: Path,
        network_name: str,
        container_name: str,
        credential_mounts: list[tuple[Path, str]],
    ) -> list[str]:
        mapping = {
            "workspace": "/workspace",
            "prompt_file": "/input/task.txt",
            "mcp_config": "/input/mcp.json",
            "result_file": "/output/agent-result.json",
        }
        command = [argument.format_map(mapping) for argument in spec.command]
        arguments = [
            "--rm",
            "--name",
            container_name,
            "--network",
            network_name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            f"{spec.memory_mb}m",
            "--cpus",
            str(spec.cpus),
            "--user",
            spec.user,
            "--workdir",
            "/workspace",
            "--tmpfs",
            _AGENT_TMPFS,
            "--tmpfs",
            "/home/agent:rw,nosuid,size=256m",
            "--env",
            "HOME=/home/agent",
            "--mount",
            bind_mount(workspace.path, "/workspace", read_only=False),
            "--mount",
            bind_mount(prompt, "/input/task.txt", read_only=True),
            "--mount",
            bind_mount(config, "/input/mcp.json", read_only=True),
            "--mount",
            bind_mount(result_path, "/output/agent-result.json", read_only=False),
        ]
        for name in spec.environment_variables:
            arguments.extend(["--env", name])
        if spec.allow_unconfined_seccomp_for_nested_sandbox:
            arguments.extend(["--security-opt", "seccomp=unconfined"])
        for source, destination in credential_mounts:
            arguments.extend(["--mount", bind_mount(source, destination, read_only=True)])
        return [*arguments, spec.image, *command]

    @staticmethod
    def _resolve_credential_mounts(
        spec: AgentRuntimeSpec,
        workspace: MaterializedWorkspace,
    ) -> list[tuple[Path, str]]:
        workspace_root = workspace.path.resolve(strict=True)
        resolved: list[tuple[Path, str]] = []
        for mount in spec.credential_mounts:
            configured_source = Path(os.environ[mount.source_environment])
            if not configured_source.is_absolute():
                raise AgentExecutionError(
                    f"credential path from {mount.source_environment} must be absolute"
                )
            if configured_source.is_symlink():
                raise AgentExecutionError("credential mounts must be regular non-link files")
            try:
                source = configured_source.resolve(strict=True)
            except OSError as exc:
                raise AgentExecutionError(
                    f"credential path from {mount.source_environment} is unavailable"
                ) from exc
            if not source.is_file():
                raise AgentExecutionError("credential mounts must be regular non-link files")
            if source.stat().st_size > _MAX_CREDENTIAL_BYTES:
                raise AgentExecutionError("credential mount exceeds the 2 MiB limit")
            try:
                source.relative_to(workspace_root)
            except ValueError:
                pass
            else:
                raise AgentExecutionError("credential source must be outside the agent workspace")
            resolved.append((source, mount.destination))
        return resolved


def _load_agent_output(path: Path) -> AgentOutput:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise AgentExecutionError("agent did not create its structured result file")
    if path.stat().st_size == 0:
        raise AgentExecutionError("agent did not create its structured result file")
    if path.stat().st_size > 1_000_000:
        raise AgentExecutionError("agent result exceeds 1 MiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentExecutionError("agent result is not valid UTF-8 JSON") from exc
    return AgentOutput.model_validate(value)


__all__ = [
    "AgentEvidenceType",
    "AgentExecutionError",
    "AgentExecutionEvidence",
    "AgentOutput",
    "AgentRuntimeSpec",
    "CredentialMountSpec",
    "DockerAgentExecutor",
    "NetworkAccess",
]
