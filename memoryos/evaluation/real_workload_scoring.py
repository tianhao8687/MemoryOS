from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from memoryos.evaluation.real_workload_containers import (
    ContainerCommandResult,
    DockerEngine,
    bind_mount,
    default_container_user,
    prepare_writable_bind_mount,
)
from memoryos.evaluation.real_workload_models import (
    HiddenTestSpec,
    MemoryExpectation,
    MemorySeedSpec,
)
from memoryos.evaluation.real_workload_workspace import (
    CapturedPatch,
    MaterializedWorkspace,
    RepositoryWorkspaceManager,
    WorkspaceError,
)

_TEST_TMPFS = "/tmp:rw,noexec,nosuid,size=512m"  # noqa: S108 - container-only tmpfs


class ScoringEngine(Protocol):
    def remove_container(self, name: str) -> None: ...

    def run_attached(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        max_log_bytes: int,
    ) -> ContainerCommandResult: ...


@dataclass(frozen=True)
class HiddenTestResult:
    success: bool
    image: str
    command_sha256: str
    expected_exit_code: int
    actual_exit_code: int | None
    hidden_patch_sha256: str | None
    hidden_patch_applied: bool
    setup_error_code: str | None
    container: ContainerCommandResult | None
    network: str = "none"
    read_only_root: bool = True
    capabilities_dropped: bool = True
    no_new_privileges: bool = True

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.container is not None:
            payload["container"]["stdout_path"] = str(self.container.stdout_path)
            payload["container"]["stderr_path"] = str(self.container.stderr_path)
        return payload


@dataclass(frozen=True)
class LeakageFinding:
    seed_id: str
    expectation: MemoryExpectation
    surface: str
    canary_sha256: str


@dataclass(frozen=True)
class LeakageReport:
    cross_project_leaks: int
    stale_memory_uses: int
    findings: tuple[LeakageFinding, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cross_project_leaks": self.cross_project_leaks,
            "stale_memory_uses": self.stale_memory_uses,
            "findings": [
                {
                    **asdict(finding),
                    "expectation": finding.expectation.value,
                }
                for finding in self.findings
            ],
        }


class HiddenTestRunner:
    def __init__(
        self,
        workspace_manager: RepositoryWorkspaceManager,
        engine: ScoringEngine | None = None,
        bind_source_resolver: Callable[[Path], Path] | None = None,
    ) -> None:
        self.workspace_manager = workspace_manager
        self.engine = engine or DockerEngine()
        self.bind_source_resolver = bind_source_resolver or (lambda path: path)

    def run(
        self,
        workspace: MaterializedWorkspace,
        spec: HiddenTestSpec,
        *,
        hidden_root: Path,
        output_dir: Path,
        container_user: str | None = None,
    ) -> HiddenTestResult:
        output = output_dir.resolve()
        if output.exists():
            raise ValueError(f"refusing to reuse scoring output directory: {output}")
        output.mkdir(parents=True)
        command_hash = hashlib.sha256(
            json.dumps(spec.command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        applied = False
        if spec.hidden_patch is not None:
            try:
                patch = self._hidden_patch(hidden_root, spec)
                self.workspace_manager.apply_captured_patch(workspace, patch)
                applied = True
            except (OSError, WorkspaceError, ValueError):
                return HiddenTestResult(
                    success=False,
                    image=spec.image,
                    command_sha256=command_hash,
                    expected_exit_code=spec.expected_exit_code,
                    actual_exit_code=None,
                    hidden_patch_sha256=spec.hidden_patch_sha256,
                    hidden_patch_applied=False,
                    setup_error_code="hidden_patch_apply_failed",
                    container=None,
                )
        name = f"memoryos-score-{uuid.uuid4().hex[:12]}"
        prepare_writable_bind_mount(
            workspace.path,
            container_user or default_container_user(),
            recursive=True,
        )
        docker_workspace = self.bind_source_resolver(workspace.path)
        arguments = [
            "--rm",
            "--name",
            name,
            "--network",
            "none",
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
            container_user or default_container_user(),
            "--workdir",
            "/workspace",
            "--tmpfs",
            _TEST_TMPFS,
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--mount",
            bind_mount(docker_workspace, "/workspace", read_only=False),
            spec.image,
            *spec.command,
        ]
        try:
            container = self.engine.run_attached(
                arguments,
                timeout_seconds=spec.timeout_seconds,
                stdout_path=output / "hidden-test.stdout.log",
                stderr_path=output / "hidden-test.stderr.log",
                max_log_bytes=2 * 1024 * 1024,
            )
        finally:
            self.engine.remove_container(name)
        return HiddenTestResult(
            success=not container.timed_out and container.exit_code == spec.expected_exit_code,
            image=spec.image,
            command_sha256=command_hash,
            expected_exit_code=spec.expected_exit_code,
            actual_exit_code=container.exit_code,
            hidden_patch_sha256=spec.hidden_patch_sha256,
            hidden_patch_applied=applied,
            setup_error_code="timeout" if container.timed_out else None,
            container=container,
        )

    @staticmethod
    def _hidden_patch(hidden_root: Path, spec: HiddenTestSpec) -> CapturedPatch:
        assert spec.hidden_patch is not None
        assert spec.hidden_patch_sha256 is not None
        root = hidden_root.resolve(strict=True)
        path = (root / spec.hidden_patch).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("hidden patch escapes hidden root") from exc
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != spec.hidden_patch_sha256:
            raise ValueError("hidden patch digest mismatch")
        return CapturedPatch(
            path=path,
            sha256=digest,
            size_bytes=len(content),
            changed_files=(),
        )


def scan_canary_leakage(
    seeds: list[MemorySeedSpec],
    *,
    patch_path: Path,
    text_surfaces: dict[str, str] | None = None,
    file_surfaces: dict[str, Path] | None = None,
) -> LeakageReport:
    surfaces: dict[str, bytes] = {"agent_patch": patch_path.read_bytes()}
    for name, text in (text_surfaces or {}).items():
        surfaces[name] = text.encode("utf-8")
    for name, path in (file_surfaces or {}).items():
        if path.exists() and path.stat().st_size <= 20 * 1024 * 1024:
            surfaces[name] = path.read_bytes()
    findings: list[LeakageFinding] = []
    for seed in seeds:
        if seed.canary is None or seed.expectation not in {
            MemoryExpectation.CROSS_PROJECT_GUARD,
            MemoryExpectation.STALE,
        }:
            continue
        encoded = seed.canary.lower().encode("utf-8")
        for surface, content in surfaces.items():
            if encoded not in content.lower():
                continue
            findings.append(
                LeakageFinding(
                    seed_id=seed.id,
                    expectation=seed.expectation,
                    surface=surface,
                    canary_sha256=hashlib.sha256(seed.canary.encode("utf-8")).hexdigest(),
                )
            )
    return LeakageReport(
        cross_project_leaks=sum(
            finding.expectation is MemoryExpectation.CROSS_PROJECT_GUARD for finding in findings
        ),
        stale_memory_uses=sum(
            finding.expectation is MemoryExpectation.STALE for finding in findings
        ),
        findings=tuple(findings),
    )


__all__ = [
    "HiddenTestResult",
    "HiddenTestRunner",
    "LeakageFinding",
    "LeakageReport",
    "scan_canary_leakage",
]
