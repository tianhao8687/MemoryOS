from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from memoryos.evaluation.real_workload_models import (
    ExperimentCondition,
    RepositorySpec,
    WorkloadTaskSpec,
)


class WorkspaceError(RuntimeError):
    """Raised when a benchmark repository cannot be isolated safely."""


@dataclass(frozen=True)
class PreparedRepository:
    repository_id: str
    mirror_path: Path
    source_fingerprint: str


@dataclass(frozen=True)
class MaterializedWorkspace:
    repository_id: str
    task_id: str
    condition: ExperimentCondition
    path: Path
    base_commit: str
    git_control_sha256: str


@dataclass(frozen=True)
class CapturedPatch:
    path: Path
    sha256: str
    size_bytes: int
    changed_files: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


class RepositoryWorkspaceManager:
    def __init__(
        self,
        root: Path,
        *,
        executable: Path | None = None,
        command_timeout_seconds: int = 180,
        refresh_existing_cache: bool = True,
        include_condition_in_workspace_path: bool = True,
    ) -> None:
        self.root = root.resolve()
        self.cache_root = self.root / "cache"
        self.runs_root = self.root / "runs"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.executable = (executable or _find_git()).resolve()
        self.command_timeout_seconds = command_timeout_seconds
        self.refresh_existing_cache = refresh_existing_cache
        self.include_condition_in_workspace_path = include_condition_in_workspace_path

    def prepare_repository(self, repository: RepositorySpec) -> PreparedRepository:
        mirror = self.cache_root / f"{repository.id}.git"
        if mirror.exists():
            if not mirror.is_dir():
                raise WorkspaceError(f"repository cache path is not a directory: {mirror}")
            origin = self._git_bare(mirror, "config", "--get", "remote.origin.url").strip()
            if _source_fingerprint(origin) != _source_fingerprint(repository.clone_url):
                raise WorkspaceError(
                    f"repository cache origin mismatch for {repository.id}; use a fresh cache root"
                )
            if self.refresh_existing_cache:
                self._git_bare(mirror, "fetch", "--prune", "--no-recurse-submodules", "origin")
        else:
            if not self.refresh_existing_cache:
                raise WorkspaceError(
                    f"repository cache is required when refresh is disabled: {repository.id}"
                )
            self._run(
                self.cache_root,
                "clone",
                "--mirror",
                "--no-tags",
                "--",
                repository.clone_url,
                str(mirror),
            )
        self._git_bare(mirror, "config", "remote.origin.tagOpt", "--no-tags")
        return PreparedRepository(
            repository_id=repository.id,
            mirror_path=mirror.resolve(strict=True),
            source_fingerprint=_source_fingerprint(repository.clone_url),
        )

    def assert_manifest_commits(
        self,
        prepared: PreparedRepository,
        tasks: list[WorkloadTaskSpec],
    ) -> None:
        for task in tasks:
            if task.repository_id != prepared.repository_id:
                continue
            self._require_bare_commit(
                prepared.mirror_path,
                task.base_commit,
                f"task {task.id} base",
            )
            if task.solution_commit:
                self._require_bare_commit(
                    prepared.mirror_path,
                    task.solution_commit,
                    f"task {task.id} solution",
                )

    def materialize(
        self,
        prepared: PreparedRepository,
        task: WorkloadTaskSpec,
        condition: ExperimentCondition,
        *,
        run_id: str,
    ) -> MaterializedWorkspace:
        safe_run_id = _safe_component(run_id, label="run_id")
        if task.repository_id != prepared.repository_id:
            raise WorkspaceError("task and prepared repository ids do not match")
        workspace_parent = self.runs_root / safe_run_id / task.id
        if self.include_condition_in_workspace_path:
            workspace_parent /= condition.value
        workspace = workspace_parent / "workspace"
        if workspace.exists():
            raise WorkspaceError(f"refusing to reuse an existing benchmark workspace: {workspace}")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._git(workspace.parent, "init", "--quiet", str(workspace))
        # Benchmark run/task/condition components can make otherwise valid repository
        # paths exceed Git for Windows' legacy 260-character boundary. Keep the setting
        # local to the disposable repository so checkout and scoring see the same tree.
        self._git(workspace, "config", "core.longpaths", "true")
        self._git(workspace, "config", "core.autocrlf", "false")
        self._git(workspace, "config", "core.eol", "lf")
        self._git(
            workspace,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            str(prepared.mirror_path),
            task.base_commit,
        )
        self._git(workspace, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        fetch_head = workspace / ".git" / "FETCH_HEAD"
        if fetch_head.exists():
            fetch_head.unlink()
        resolved = self._git(workspace, "rev-parse", "HEAD").strip()
        if resolved != task.base_commit:
            raise WorkspaceError(f"workspace HEAD mismatch for task {task.id}")
        remotes = self._git(workspace, "remote").strip()
        if remotes:
            raise WorkspaceError(f"sanitized workspace unexpectedly has a remote: {remotes}")
        if task.solution_commit and self._object_exists(workspace, task.solution_commit):
            raise WorkspaceError(
                f"sanitized workspace for task {task.id} contains the hidden solution object"
            )
        if self._git(workspace, "status", "--porcelain=v1").strip():
            raise WorkspaceError(f"materialized workspace for task {task.id} is not clean")
        return MaterializedWorkspace(
            repository_id=prepared.repository_id,
            task_id=task.id,
            condition=condition,
            path=workspace.resolve(strict=True),
            base_commit=resolved,
            git_control_sha256=_git_control_plane_digest(workspace),
        )

    def capture_patch(
        self,
        workspace: MaterializedWorkspace,
        output_path: Path,
        *,
        max_patch_bytes: int = 20 * 1024 * 1024,
        max_changed_files: int = 2000,
        max_changed_file_bytes: int = 5 * 1024 * 1024,
    ) -> CapturedPatch:
        self._validate_agent_git_control_plane(workspace)
        changed_files = self._changed_paths(workspace.path, workspace.base_commit)
        if len(changed_files) > max_changed_files:
            raise WorkspaceError("agent changed too many files")
        total_size = 0
        for relative in changed_files:
            path = _resolve_worktree_path(workspace.path, relative)
            if not path.exists() and not path.is_symlink():
                continue
            if not path.is_file() and not path.is_symlink():
                raise WorkspaceError(f"agent changed an unsupported file type: {relative}")
            size = path.lstat().st_size
            if size > max_changed_file_bytes:
                raise WorkspaceError(f"agent-created file exceeds size limit: {relative}")
            total_size += size
            if total_size > max_patch_bytes:
                raise WorkspaceError("agent changes exceed the aggregate size limit")

        self._git(workspace.path, "add", "-A", "--", ".", hardened=True)
        patch = self._run_bytes(
            workspace.path,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            workspace.base_commit,
            "--",
            hardened=True,
        ).stdout
        if len(patch) > max_patch_bytes:
            raise WorkspaceError("captured patch exceeds size limit")
        destination = output_path.resolve()
        if _is_within(destination, workspace.path):
            raise WorkspaceError("captured patch must be stored outside the agent workspace")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(patch)
        return CapturedPatch(
            path=destination,
            sha256=hashlib.sha256(patch).hexdigest(),
            size_bytes=len(patch),
            changed_files=tuple(changed_files),
        )

    def apply_captured_patch(
        self,
        workspace: MaterializedWorkspace,
        patch: CapturedPatch,
    ) -> None:
        self._validate_agent_git_control_plane(workspace)
        content = patch.path.read_bytes()
        if hashlib.sha256(content).hexdigest() != patch.sha256:
            raise WorkspaceError("captured patch digest mismatch")
        if not content:
            return
        self._run_bytes(
            workspace.path,
            "apply",
            "--index",
            "--binary",
            "--whitespace=nowarn",
            "-",
            input_bytes=content,
            hardened=True,
        )

    def _changed_paths(self, root: Path, base_commit: str) -> list[str]:
        tracked = self._run_bytes(
            root,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
            "--",
            hardened=True,
        ).stdout
        raw = self._run_bytes(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            hardened=True,
        ).stdout
        paths = [
            _normalize_git_path(os.fsdecode(value)) for value in tracked.split(b"\x00") if value
        ]
        for record in raw.split(b"\x00"):
            if not record:
                continue
            value = record[3:] if len(record) >= 3 and record[2:3] == b" " else record
            decoded = os.fsdecode(value)
            normalized = _normalize_git_path(decoded)
            if normalized not in paths:
                paths.append(normalized)
        return sorted(paths)

    @staticmethod
    def _validate_agent_git_control_plane(workspace: MaterializedWorkspace) -> None:
        actual = _git_control_plane_digest(workspace.path)
        if not workspace.git_control_sha256 or actual != workspace.git_control_sha256:
            raise WorkspaceError("agent modified the Git control plane")

    def _object_exists(self, root: Path, commit: str) -> bool:
        result = self._run(
            root,
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
            check=False,
        )
        if result.returncode not in {0, 1, 128}:
            raise WorkspaceError(result.stderr.strip() or "git cat-file failed")
        return result.returncode == 0

    def _require_bare_commit(self, mirror: Path, commit: str, label: str) -> None:
        result = self._run(
            self.cache_root,
            f"--git-dir={mirror}",
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
            check=False,
        )
        if result.returncode != 0:
            raise WorkspaceError(f"{label} commit is unavailable in the repository mirror")

    def _git_bare(self, mirror: Path, *arguments: str) -> str:
        return self._run(self.cache_root, f"--git-dir={mirror}", *arguments).stdout

    def _git(self, root: Path, *arguments: str, hardened: bool = False) -> str:
        return self._run(root, *arguments, hardened=hardened).stdout

    def _run(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
        hardened: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.executable), *arguments]
        environment = None
        if hardened:
            command = [
                str(self.executable),
                "-c",
                f"core.hooksPath={self._empty_hooks_directory()}",
                *arguments,
            ]
            environment = _hardened_git_environment()
        try:
            return subprocess.run(  # noqa: S603 - fixed git executable and argv-only invocation
                command,
                cwd=root,
                check=check,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout_seconds,
                env=environment,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorkspaceError(f"git command failed: {detail}") from exc

    def _run_bytes(
        self,
        root: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
        hardened: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [str(self.executable), *arguments]
        environment = None
        if hardened:
            command = [
                str(self.executable),
                "-c",
                f"core.hooksPath={self._empty_hooks_directory()}",
                *arguments,
            ]
            environment = _hardened_git_environment()
        try:
            return subprocess.run(  # noqa: S603 - fixed git executable and argv-only invocation
                command,
                cwd=root,
                check=True,
                capture_output=True,
                input=input_bytes,
                timeout=self.command_timeout_seconds,
                env=environment,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", None)
            message = os.fsdecode(detail) if isinstance(detail, bytes) else str(detail or exc)
            raise WorkspaceError(f"git command failed: {message}") from exc

    def _empty_hooks_directory(self) -> Path:
        directory = self.root / "trusted-empty-hooks"
        directory.mkdir(exist_ok=True)
        return directory


def _find_git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise WorkspaceError("git executable is required for real-workload execution")
    return Path(executable)


def _source_fingerprint(source: str) -> str:
    normalized = str(Path(source).resolve()) if _looks_local(source) else source.rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _looks_local(source: str) -> bool:
    return "://" not in source and not source.startswith("git@")


def _safe_component(value: str, *, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._-"
    if not value or any(character not in allowed for character in value):
        raise WorkspaceError(f"{label} contains unsafe path characters")
    return value


def _normalize_git_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise WorkspaceError("git reported an unsafe changed path")
    return path.as_posix()


def _resolve_worktree_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    parent = candidate.parent.resolve()
    if not _is_within(parent, root):
        raise WorkspaceError("changed path escapes the agent workspace")
    return candidate


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _git_control_plane_digest(root: Path) -> str:
    git_directory = root / ".git"
    if not git_directory.is_dir() or _is_link_like(git_directory):
        raise WorkspaceError("workspace .git must be a real directory")
    _reject_git_links(git_directory)
    config = git_directory / "config"
    if not config.is_file() or _is_link_like(config):
        raise WorkspaceError("workspace .git/config must be a regular file")
    alternates = git_directory / "objects" / "info" / "alternates"
    if alternates.exists() or _is_link_like(alternates):
        raise WorkspaceError("workspace Git object alternates are forbidden")

    digest = hashlib.sha256()
    for entry in [
        config,
        git_directory / "config.worktree",
        git_directory / "hooks",
        git_directory / "info",
    ]:
        if not entry.exists():
            digest.update(f"missing:{entry.name}\0".encode())
            continue
        if _is_link_like(entry):
            raise WorkspaceError("workspace Git control paths must not be links")
        candidates = [entry] if entry.is_file() else list(_walk_control_path(entry))
        for candidate in candidates:
            if _is_link_like(candidate):
                raise WorkspaceError("workspace Git control paths must not contain links")
            relative = candidate.relative_to(git_directory).as_posix()
            kind = "directory" if candidate.is_dir() else "file"
            digest.update(f"{kind}:{relative}\0".encode())
            if candidate.is_file():
                digest.update(candidate.read_bytes())
                digest.update(b"\0")
    return digest.hexdigest()


def _walk_control_path(root: Path) -> list[Path]:
    entries: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        entries.append(child)
        if child.is_dir() and not _is_link_like(child):
            entries.extend(_walk_control_path(child))
    return entries


def _reject_git_links(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            if _is_link_like(child):
                raise WorkspaceError("workspace .git must not contain links")
            if child.is_dir():
                pending.append(child)
            elif not child.is_file():
                raise WorkspaceError("workspace .git must contain only files and directories")


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _hardened_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    return environment


__all__ = [
    "CapturedPatch",
    "MaterializedWorkspace",
    "PreparedRepository",
    "RepositoryWorkspaceManager",
    "WorkspaceError",
]
