from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from memoryos.domain.schemas import MemoryType

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$",
    flags=re.IGNORECASE,
)
_SPDX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,79}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "pwsh",
    "pwsh.exe",
    "powershell",
    "powershell.exe",
    "sh",
    "zsh",
}


class DatasetTier(StrEnum):
    HARNESS_FIXTURE = "harness_fixture"
    PUBLIC_REPLAY = "public_replay"
    PRIVATE_OPT_IN = "private_opt_in"


class MemoryExpectation(StrEnum):
    HELPFUL = "helpful"
    IRRELEVANT = "irrelevant"
    STALE = "stale"
    CROSS_PROJECT_GUARD = "cross_project_guard"


class ExperimentCondition(StrEnum):
    NO_MEMORY = "no_memory"
    FLAT_MEMORY = "flat_memory"
    MEMORYOS = "memoryos"


Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN.pattern)]
CommitSha = Annotated[str, Field(pattern=_COMMIT_PATTERN.pattern)]


class _HasIdentifier(Protocol):
    @property
    def id(self) -> str: ...


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return value.astimezone(UTC)


def _validate_relative_path(value: str, *, field_name: str) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"{field_name} must be a traversal-free relative path")
    if any(part in {"", "."} for part in candidate.parts):
        raise ValueError(f"{field_name} must be a normalized relative path")
    return candidate.as_posix()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositorySpec(StrictModel):
    id: Identifier
    clone_url: str = Field(min_length=1, max_length=2000)
    source_url: AnyHttpUrl | None = None
    license_spdx: str = Field(min_length=1, max_length=80)
    license_url: AnyHttpUrl | None = None

    @field_validator("clone_url")
    @classmethod
    def reject_embedded_clone_credentials(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("clone_url must not contain NUL bytes")
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise ValueError("clone_url must not embed credentials")
        return value

    @field_validator("license_spdx")
    @classmethod
    def validate_spdx_identifier(cls, value: str) -> str:
        if not _SPDX_PATTERN.fullmatch(value):
            raise ValueError("license_spdx must be a single SPDX identifier")
        return value


class MemorySeedSpec(StrictModel):
    id: Identifier
    repository_id: Identifier
    memory_type: MemoryType = MemoryType.PROJECT
    category: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20_000)
    captured_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_commit: CommitSha | None = None
    source_ref: str = Field(min_length=1, max_length=2000)
    expectation: MemoryExpectation = MemoryExpectation.HELPFUL
    canary: str | None = Field(default=None, min_length=8, max_length=120)
    confidence: float = Field(default=0.9, ge=0, le=1)
    importance: float = Field(default=0.7, ge=0, le=1)

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="captured_at")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def validate_optional_time(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_valid_window(self) -> MemorySeedSpec:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        if self.canary is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,119}", self.canary):
                raise ValueError("canary must be an unambiguous portable token")
            if self.canary not in self.content:
                raise ValueError("canary must occur verbatim in memory content")
        if self.expectation is MemoryExpectation.CROSS_PROJECT_GUARD and self.canary is None:
            raise ValueError("cross_project_guard memories require a canary")
        return self


class HiddenTestSpec(StrictModel):
    image: str = Field(min_length=1, max_length=500)
    command: list[str] = Field(min_length=1, max_length=100)
    hidden_patch: str | None = None
    hidden_patch_sha256: str | None = None
    expected_exit_code: int = Field(default=0, ge=0, le=255)
    timeout_seconds: int = Field(default=180, ge=1, le=1800)
    memory_mb: int = Field(default=1024, ge=128, le=16_384)
    cpus: float = Field(default=1.0, gt=0, le=8)
    pids_limit: int = Field(default=256, ge=16, le=4096)
    network: Literal["none"] = "none"
    read_only_root: Literal[True] = True

    @field_validator("image")
    @classmethod
    def require_digest_pinned_image(cls, value: str) -> str:
        if not _IMAGE_PATTERN.fullmatch(value):
            raise ValueError("image must be pinned by a sha256 digest")
        return value

    @field_validator("command")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("command must contain non-empty argv values without NUL bytes")
        executable = PurePosixPath(value[0].replace("\\", "/")).name.lower()
        if executable in _FORBIDDEN_EXECUTABLES:
            raise ValueError("hidden tests must use direct argv execution, not a shell")
        return value

    @field_validator("hidden_patch")
    @classmethod
    def validate_hidden_patch(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_relative_path(value, field_name="hidden_patch")

    @field_validator("hidden_patch_sha256")
    @classmethod
    def validate_patch_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("hidden_patch_sha256 must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def require_patch_digest_pair(self) -> HiddenTestSpec:
        if (self.hidden_patch is None) != (self.hidden_patch_sha256 is None):
            raise ValueError("hidden_patch and hidden_patch_sha256 must be set together")
        return self


class WorkloadTaskSpec(StrictModel):
    id: Identifier
    repository_id: Identifier
    sequence_id: Identifier
    sequence_index: int = Field(ge=0)
    base_commit: CommitSha
    solution_commit: CommitSha | None = None
    cutoff: datetime
    source_url: AnyHttpUrl | None = None
    source_published_at: datetime | None = None
    prompt: str = Field(min_length=1, max_length=20_000)
    memory_seed_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    hidden_test: HiddenTestSpec
    tags: list[Identifier] = Field(default_factory=list, max_length=50)

    @field_validator("cutoff")
    @classmethod
    def validate_cutoff(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="cutoff")

    @field_validator("source_published_at")
    @classmethod
    def validate_source_published_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, field_name="source_published_at")

    @model_validator(mode="after")
    def prevent_direct_solution_leak(self) -> WorkloadTaskSpec:
        if self.solution_commit and self.solution_commit in self.prompt.lower():
            raise ValueError("prompt contains the hidden solution commit")
        if len(set(self.memory_seed_ids)) != len(self.memory_seed_ids):
            raise ValueError("memory_seed_ids must be unique per task")
        return self


class RealWorkloadManifest(StrictModel):
    schema_version: Literal["2.2"] = "2.2"
    name: Identifier
    tier: DatasetTier
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consent_record: str | None = Field(default=None, max_length=2000)
    repositories: list[RepositorySpec] = Field(min_length=1)
    memories: list[MemorySeedSpec] = Field(default_factory=list)
    tasks: list[WorkloadTaskSpec] = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_references_and_provenance(self) -> RealWorkloadManifest:
        repositories = _unique_by_id(self.repositories, kind="repository")
        memories = _unique_by_id(self.memories, kind="memory")
        _unique_by_id(self.tasks, kind="task")

        if self.tier is DatasetTier.PRIVATE_OPT_IN and not self.consent_record:
            raise ValueError("private_opt_in datasets require a consent_record")

        for repository in self.repositories:
            if self.tier is DatasetTier.PUBLIC_REPLAY:
                _require_https(repository.clone_url, field_name="clone_url")
                if repository.source_url is None or repository.license_url is None:
                    raise ValueError(
                        "public_replay repositories require source_url and license_url"
                    )
                _require_https(str(repository.source_url), field_name="source_url")
                _require_https(str(repository.license_url), field_name="license_url")

        sequence_positions: set[tuple[str, int]] = set()
        for memory in self.memories:
            if memory.repository_id not in repositories:
                raise ValueError(f"memory {memory.id} references an unknown repository")
            if self.tier is DatasetTier.PUBLIC_REPLAY and memory.source_commit is None:
                raise ValueError(f"public memory {memory.id} requires source_commit provenance")

        for task in self.tasks:
            task_repository = repositories.get(task.repository_id)
            if task_repository is None:
                raise ValueError(f"task {task.id} references an unknown repository")
            position = (task.sequence_id, task.sequence_index)
            if position in sequence_positions:
                raise ValueError("sequence_id and sequence_index pairs must be unique")
            sequence_positions.add(position)
            if self.tier is DatasetTier.PUBLIC_REPLAY:
                if (
                    task.source_url is None
                    or task.source_published_at is None
                    or task.solution_commit is None
                ):
                    raise ValueError(
                        f"public replay task {task.id} requires source_url, "
                        "source_published_at, and solution_commit"
                    )
                _require_https(str(task.source_url), field_name="task source_url")
                if task.source_published_at > task.cutoff:
                    raise ValueError(f"public replay task {task.id} was published after its cutoff")
            for memory_id in task.memory_seed_ids:
                task_memory = memories.get(memory_id)
                if task_memory is None:
                    raise ValueError(f"task {task.id} references unknown memory {memory_id}")
                if task_memory.captured_at > task.cutoff:
                    raise ValueError(f"memory {memory_id} was captured after task {task.id} cutoff")
                if task_memory.repository_id != task.repository_id and (
                    task_memory.expectation is not MemoryExpectation.CROSS_PROJECT_GUARD
                ):
                    raise ValueError(
                        f"cross-project memory {memory_id} must be labelled cross_project_guard"
                    )
                if task.solution_commit and _contains_commit(task_memory, task.solution_commit):
                    raise ValueError(f"memory {memory_id} contains task {task.id} solution commit")
        referenced_repositories = {
            *(memory.repository_id for memory in self.memories),
            *(task.repository_id for task in self.tasks),
        }
        unused_repositories = sorted(set(repositories) - referenced_repositories)
        if unused_repositories:
            raise ValueError(f"manifest contains unused repositories: {unused_repositories}")
        referenced_memories = {
            memory_id for task in self.tasks for memory_id in task.memory_seed_ids
        }
        unused_memories = sorted(set(memories) - referenced_memories)
        if unused_memories:
            raise ValueError(f"manifest contains unused memories: {unused_memories}")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _unique_by_id[T: _HasIdentifier](items: list[T], *, kind: str) -> dict[str, T]:
    indexed: dict[str, T] = {}
    for item in items:
        if item.id in indexed:
            raise ValueError(f"duplicate {kind} id: {item.id}")
        indexed[item.id] = item
    return indexed


def _require_https(value: str, *, field_name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"public_replay {field_name} must use https")
    if parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError(f"public_replay {field_name} must not contain credentials or a query")


def _contains_commit(memory: MemorySeedSpec, commit: str) -> bool:
    values = [memory.content, memory.title, memory.source_ref, memory.source_commit or ""]
    return any(commit in value.lower() for value in values)


def load_real_workload_manifest(path: Path) -> RealWorkloadManifest:
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON manifest: {path}") from exc
    return TypeAdapter(RealWorkloadManifest).validate_python(decoded)


__all__ = [
    "DatasetTier",
    "ExperimentCondition",
    "HiddenTestSpec",
    "MemoryExpectation",
    "MemorySeedSpec",
    "RealWorkloadManifest",
    "RepositorySpec",
    "WorkloadTaskSpec",
    "load_real_workload_manifest",
]
