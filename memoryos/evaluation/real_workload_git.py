from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memoryos.evaluation.real_workload_models import (
    MemoryExpectation,
    MemorySeedSpec,
    RealWorkloadManifest,
    WorkloadTaskSpec,
)


class GitValidationError(ValueError):
    """Raised when repository history violates the benchmark's temporal contract."""


@dataclass(frozen=True)
class TemporalValidationReport:
    repository_id: str
    repository_path: str
    manifest_digest: str
    checked_task_ids: tuple[str, ...]
    checked_commits: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class GitHistoryInspector:
    def __init__(self, *, executable: Path | None = None, timeout_seconds: int = 30) -> None:
        resolved = executable or _find_git()
        self.executable = resolved.resolve()
        self.timeout_seconds = timeout_seconds

    def validate_repository(
        self,
        repository_path: Path,
        manifest: RealWorkloadManifest,
        repository_id: str,
    ) -> TemporalValidationReport:
        root = repository_path.resolve(strict=True)
        self._ensure_repository(root)
        repository_ids = {repository.id for repository in manifest.repositories}
        if repository_id not in repository_ids:
            raise GitValidationError(f"unknown repository id: {repository_id}")

        checked: set[str] = set()
        task_ids: list[str] = []
        memories = {memory.id: memory for memory in manifest.memories}
        source_times = self._validate_memory_sources(
            root,
            [memory for memory in manifest.memories if memory.repository_id == repository_id],
            checked,
        )
        for task in manifest.tasks:
            if task.repository_id != repository_id:
                continue
            task_ids.append(task.id)
            self._validate_task(root, task)
            checked.add(task.base_commit)
            if task.solution_commit:
                checked.add(task.solution_commit)
        self._validate_memory_references(
            root,
            manifest,
            repository_id,
            memories,
            source_times,
        )

        if not task_ids:
            raise GitValidationError(f"manifest has no tasks for repository {repository_id}")
        return TemporalValidationReport(
            repository_id=repository_id,
            repository_path=str(root),
            manifest_digest=manifest.digest(),
            checked_task_ids=tuple(task_ids),
            checked_commits=tuple(sorted(checked)),
        )

    def validate_memory_repository(
        self,
        repository_path: Path,
        manifest: RealWorkloadManifest,
        repository_id: str,
    ) -> TemporalValidationReport:
        """Validate a repository used only as provenance for cross-project memories."""

        root = repository_path.resolve(strict=True)
        self._ensure_repository(root)
        repository_ids = {repository.id for repository in manifest.repositories}
        if repository_id not in repository_ids:
            raise GitValidationError(f"unknown repository id: {repository_id}")
        if any(task.repository_id == repository_id for task in manifest.tasks):
            raise GitValidationError(
                f"repository {repository_id} has tasks; use validate_repository"
            )
        repository_memories = [
            memory for memory in manifest.memories if memory.repository_id == repository_id
        ]
        if not repository_memories:
            raise GitValidationError(f"manifest has no memories for repository {repository_id}")
        checked: set[str] = set()
        source_times = self._validate_memory_sources(root, repository_memories, checked)
        memories = {memory.id: memory for memory in manifest.memories}
        self._validate_memory_references(
            root,
            manifest,
            repository_id,
            memories,
            source_times,
        )
        return TemporalValidationReport(
            repository_id=repository_id,
            repository_path=str(root),
            manifest_digest=manifest.digest(),
            checked_task_ids=(),
            checked_commits=tuple(sorted(checked)),
        )

    def _validate_memory_sources(
        self,
        root: Path,
        memories: list[MemorySeedSpec],
        checked: set[str],
    ) -> dict[str, datetime]:
        source_times: dict[str, datetime] = {}
        for memory in memories:
            if memory.source_commit is None:
                continue
            self._require_commit(root, memory.source_commit, label=f"memory {memory.id} source")
            source_time = self.committed_at(root, memory.source_commit)
            if source_time > memory.captured_at:
                raise GitValidationError(
                    f"memory {memory.id} was captured before its source commit existed"
                )
            source_times[memory.id] = source_time
            checked.add(memory.source_commit)
        return source_times

    def _validate_memory_references(
        self,
        root: Path,
        manifest: RealWorkloadManifest,
        repository_id: str,
        memories: dict[str, MemorySeedSpec],
        source_times: dict[str, datetime],
    ) -> None:
        for task in manifest.tasks:
            for memory_id in task.memory_seed_ids:
                memory = memories[memory_id]
                if memory.repository_id != repository_id:
                    continue
                source_time = source_times.get(memory.id)
                if source_time is not None and source_time > task.cutoff:
                    raise GitValidationError(
                        f"memory {memory.id} source commit is later than task {task.id} cutoff"
                    )
                if (
                    task.repository_id == repository_id
                    and memory.source_commit is not None
                    and not self.is_ancestor(root, memory.source_commit, task.base_commit)
                ):
                    raise GitValidationError(
                        f"memory {memory.id} source commit is not an ancestor of task "
                        f"{task.id} base commit"
                    )
                self._validate_memory_window(
                    memory.id,
                    memory.expectation,
                    memory.valid_from,
                    memory.valid_to,
                    task,
                )

    def committed_at(self, repository_path: Path, commit: str) -> datetime:
        value = self._git(repository_path, "show", "-s", "--format=%cI", commit).strip()
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise GitValidationError(f"git returned an invalid timestamp for {commit}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GitValidationError(f"git returned a timezone-free timestamp for {commit}")
        return parsed.astimezone(UTC)

    def is_ancestor(self, repository_path: Path, ancestor: str, descendant: str) -> bool:
        result = self._run(
            repository_path,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise GitValidationError(result.stderr.strip() or "git merge-base failed")
        return result.returncode == 0

    def _validate_task(self, root: Path, task: WorkloadTaskSpec) -> None:
        self._require_commit(root, task.base_commit, label=f"task {task.id} base")
        base_time = self.committed_at(root, task.base_commit)
        if base_time > task.cutoff:
            raise GitValidationError(f"task {task.id} base commit is later than its cutoff")
        if task.solution_commit is None:
            return
        self._require_commit(root, task.solution_commit, label=f"task {task.id} solution")
        if not self.is_ancestor(root, task.base_commit, task.solution_commit):
            raise GitValidationError(
                f"task {task.id} solution commit is not a descendant of its base commit"
            )
        solution_time = self.committed_at(root, task.solution_commit)
        if solution_time <= task.cutoff:
            raise GitValidationError(
                f"task {task.id} solution commit must be strictly later than its cutoff"
            )
        leak = self._run(
            root,
            "grep",
            "-I",
            "--fixed-strings",
            "-e",
            task.solution_commit,
            task.base_commit,
            "--",
            check=False,
        )
        if leak.returncode == 0:
            raise GitValidationError(f"task {task.id} base tree contains its solution commit")
        if leak.returncode not in {0, 1}:
            raise GitValidationError(leak.stderr.strip() or "git grep failed")

    @staticmethod
    def _validate_memory_window(
        memory_id: str,
        expectation: MemoryExpectation,
        valid_from: datetime | None,
        valid_to: datetime | None,
        task: WorkloadTaskSpec,
    ) -> None:
        valid_at_cutoff = (valid_from is None or valid_from <= task.cutoff) and (
            valid_to is None or valid_to > task.cutoff
        )
        if expectation is MemoryExpectation.HELPFUL and not valid_at_cutoff:
            raise GitValidationError(
                f"helpful memory {memory_id} is not valid at task {task.id} cutoff"
            )
        if expectation is MemoryExpectation.STALE and valid_at_cutoff:
            raise GitValidationError(
                f"stale memory {memory_id} is still valid at task {task.id} cutoff"
            )

    def _ensure_repository(self, root: Path) -> None:
        work_tree = self._git(root, "rev-parse", "--is-inside-work-tree").strip()
        bare = self._git(root, "rev-parse", "--is-bare-repository").strip()
        if work_tree != "true" and bare != "true":
            raise GitValidationError(f"not a git work tree: {root}")

    def _require_commit(self, root: Path, commit: str, *, label: str) -> None:
        resolved = self._git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
        if resolved != commit:
            raise GitValidationError(f"{label} does not resolve to its pinned 40-character SHA")

    def _git(self, root: Path, *arguments: str) -> str:
        return self._run(root, *arguments).stdout

    def _run(
        self,
        root: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603 - fixed executable and argv-only invocation
                [str(self.executable), *arguments],
                cwd=root,
                check=check,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise GitValidationError(f"git command failed: {detail}") from exc


def _find_git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise GitValidationError("git executable is required for real-workload validation")
    return Path(executable)


__all__ = ["GitHistoryInspector", "GitValidationError", "TemporalValidationReport"]
