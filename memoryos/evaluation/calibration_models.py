from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
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

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SPDX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,79}$")

Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN.pattern)]
CommitSha = Annotated[str, Field(pattern=_COMMIT_PATTERN.pattern)]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN.pattern)]


class CalibrationSplit(StrEnum):
    TRAIN = "train"
    DEV = "dev"
    TEST = "test"


class RepositoryRole(StrEnum):
    QUERY_SOURCE = "query_source"
    GUARD_ONLY = "guard_only"


class CandidateEligibility(StrEnum):
    ELIGIBLE = "eligible"
    FUTURE_GUARD = "future_guard"
    CROSS_SCOPE_GUARD = "cross_scope_guard"


class JudgmentOrigin(StrEnum):
    SILVER_GIT_PATH = "silver_git_path_overlap_v1"
    HUMAN_ADJUDICATED = "human_adjudicated"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return value.astimezone(UTC)


def _validate_relative_path(value: str, *, field_name: str) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"{field_name} must be a traversal-free relative path")
    if any(part in {"", "."} for part in candidate.parts):
        raise ValueError(f"{field_name} must be normalized")
    return candidate.as_posix()


def _require_public_https(value: str, *, field_name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{field_name} must use public https")
    if parsed.username is not None or parsed.password is not None or parsed.query:
        raise ValueError(f"{field_name} must not contain credentials or a query")
    return value


class CalibrationBuildSettings(StrictModel):
    history_limit: int = Field(default=800, ge=50, le=10_000)
    lookback_commits: int = Field(default=240, ge=20, le=5000)
    max_queries_per_repository: int = Field(default=50, ge=1, le=1000)
    min_queries_per_repository: int = Field(default=35, ge=1, le=1000)
    candidate_pool_size: int = Field(default=32, ge=8, le=500)
    max_positive_candidates: int = Field(default=12, ge=1, le=100)
    lexical_negative_candidates: int = Field(default=8, ge=0, le=100)
    recent_negative_candidates: int = Field(default=8, ge=0, le=100)
    future_guards_per_query: int = Field(default=1, ge=1, le=10)
    cross_scope_guards_per_query: int = Field(default=1, ge=1, le=10)
    min_query_tokens: int = Field(default=3, ge=2, le=20)
    max_target_paths: int = Field(default=12, ge=1, le=100)
    max_candidate_paths: int = Field(default=24, ge=1, le=200)

    @model_validator(mode="after")
    def validate_consistent_limits(self) -> CalibrationBuildSettings:
        if self.min_queries_per_repository > self.max_queries_per_repository:
            raise ValueError("min_queries_per_repository cannot exceed the maximum")
        if self.lookback_commits >= self.history_limit:
            raise ValueError("lookback_commits must be smaller than history_limit")
        guards = self.future_guards_per_query + self.cross_scope_guards_per_query
        if guards >= self.candidate_pool_size:
            raise ValueError("candidate_pool_size must leave room for eligible candidates")
        return self


class CalibrationRepositorySource(StrictModel):
    id: Identifier
    role: RepositoryRole
    split: CalibrationSplit | None = None
    language: Identifier
    clone_url: str = Field(min_length=1, max_length=2000)
    source_url: AnyHttpUrl
    snapshot_commit: CommitSha
    license_spdx: str = Field(min_length=1, max_length=80)
    license_path: str = Field(min_length=1, max_length=500)
    license_url: AnyHttpUrl

    @field_validator("clone_url")
    @classmethod
    def validate_clone_url(cls, value: str) -> str:
        return _require_public_https(value, field_name="clone_url")

    @field_validator("source_url", "license_url")
    @classmethod
    def validate_public_url(cls, value: AnyHttpUrl, info: Any) -> AnyHttpUrl:
        _require_public_https(str(value), field_name=info.field_name)
        return value

    @field_validator("license_spdx")
    @classmethod
    def validate_spdx(cls, value: str) -> str:
        if not _SPDX_PATTERN.fullmatch(value):
            raise ValueError("license_spdx must be one SPDX identifier")
        return value

    @field_validator("license_path")
    @classmethod
    def validate_license_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="license_path")

    @model_validator(mode="after")
    def validate_role_and_split(self) -> CalibrationRepositorySource:
        if self.role is RepositoryRole.QUERY_SOURCE and self.split is None:
            raise ValueError("query_source repositories require a split")
        if self.role is RepositoryRole.GUARD_ONLY and self.split is not None:
            raise ValueError("guard_only repositories cannot belong to a query split")
        return self


class CalibrationSourceConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: Identifier
    generator_version: Identifier = "git-silver-v1"
    generated_at: datetime
    settings: CalibrationBuildSettings = Field(default_factory=CalibrationBuildSettings)
    repositories: list[CalibrationRepositorySource] = Field(min_length=4)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_repository_roles(self) -> CalibrationSourceConfig:
        _unique_by(self.repositories, key=lambda item: item.id, kind="repository")
        query_sources = [
            repository
            for repository in self.repositories
            if repository.role is RepositoryRole.QUERY_SOURCE
        ]
        guards = [
            repository
            for repository in self.repositories
            if repository.role is RepositoryRole.GUARD_ONLY
        ]
        if not guards:
            raise ValueError("at least one guard_only repository is required")
        observed = {repository.split for repository in query_sources}
        if observed != set(CalibrationSplit):
            raise ValueError("query_source repositories must cover train, dev, and test")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class CalibrationCandidate(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    repository_id: Identifier
    source_commit: CommitSha
    committed_at: datetime
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=5000)
    source_paths: list[str] = Field(min_length=1, max_length=200)
    scope_type: Literal["repository"] = "repository"
    scope_key: Identifier
    source_url: AnyHttpUrl

    @field_validator("committed_at")
    @classmethod
    def validate_committed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="committed_at")

    @field_validator("source_paths")
    @classmethod
    def validate_source_paths(cls, value: list[str]) -> list[str]:
        normalized = [_validate_relative_path(path, field_name="source_paths") for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_paths must be unique")
        return normalized


class CalibrationQuery(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: Identifier
    repository_id: Identifier
    split: CalibrationSplit
    cutoff_commit: CommitSha
    cutoff_time: datetime
    query: str = Field(min_length=1, max_length=5000)
    candidate_ids: list[Identifier] = Field(min_length=1, max_length=500)
    challenge_tags: list[Identifier] = Field(default_factory=list, max_length=50)
    source_kind: Literal["git_commit_message_proxy"] = "git_commit_message_proxy"

    @field_validator("cutoff_time")
    @classmethod
    def validate_cutoff_time(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="cutoff_time")

    @model_validator(mode="after")
    def validate_unique_candidates(self) -> CalibrationQuery:
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        return self


class SilverJudgmentEvidence(StrictModel):
    target_commit: CommitSha
    target_paths: list[str] = Field(min_length=1, max_length=100)
    shared_paths: list[str] = Field(default_factory=list, max_length=100)
    shared_directories: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("target_paths", "shared_paths", "shared_directories")
    @classmethod
    def validate_paths(cls, value: list[str], info: Any) -> list[str]:
        normalized = [_validate_relative_path(path, field_name=info.field_name) for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must be unique")
        return normalized


class CalibrationJudgment(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    query_id: Identifier
    candidate_id: Identifier
    relevance: int = Field(ge=0, le=3)
    eligibility: CandidateEligibility
    required: bool
    forbidden: bool
    origin: JudgmentOrigin = JudgmentOrigin.SILVER_GIT_PATH
    review_status: Literal["unreviewed", "adjudicated"] = "unreviewed"
    evidence: SilverJudgmentEvidence

    @model_validator(mode="after")
    def validate_semantics(self) -> CalibrationJudgment:
        is_eligible = self.eligibility is CandidateEligibility.ELIGIBLE
        if self.forbidden == is_eligible:
            raise ValueError("forbidden must be true exactly for ineligible guard candidates")
        if not is_eligible and (self.relevance != 0 or self.required):
            raise ValueError("guard candidates must have relevance=0 and required=false")
        if self.required and self.relevance != 3:
            raise ValueError("required silver judgments must have exact-path relevance=3")
        if self.origin is JudgmentOrigin.HUMAN_ADJUDICATED and self.review_status != "adjudicated":
            raise ValueError("human judgments must be adjudicated")
        return self


class CalibrationRepositorySnapshot(StrictModel):
    id: Identifier
    role: RepositoryRole
    split: CalibrationSplit | None = None
    language: Identifier
    clone_url: str
    source_url: AnyHttpUrl
    snapshot_commit: CommitSha
    snapshot_tree: CommitSha
    snapshot_committed_at: datetime
    license_spdx: str
    license_path: str
    license_url: AnyHttpUrl
    license_sha256: Sha256
    mined_commits: int = Field(ge=1)

    @field_validator("snapshot_committed_at")
    @classmethod
    def validate_snapshot_time(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="snapshot_committed_at")


class ArtifactMetadata(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: Sha256
    rows: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="artifact path")


class SplitArtifacts(StrictModel):
    queries: ArtifactMetadata
    qrels: ArtifactMetadata


class CalibrationArtifacts(StrictModel):
    candidates: ArtifactMetadata
    train: SplitArtifacts
    dev: SplitArtifacts
    test: SplitArtifacts

    def for_split(self, split: CalibrationSplit) -> SplitArtifacts:
        if split is CalibrationSplit.TRAIN:
            return self.train
        if split is CalibrationSplit.DEV:
            return self.dev
        return self.test


class CalibrationSummary(StrictModel):
    repositories: int = Field(ge=1)
    query_repositories: int = Field(ge=1)
    candidates: int = Field(ge=1)
    queries: int = Field(ge=1)
    judgments: int = Field(ge=1)
    queries_by_split: dict[str, int]
    queries_by_repository: dict[str, int]
    relevance_counts: dict[str, int]
    eligibility_counts: dict[str, int]


class CalibrationDatasetManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: Identifier
    label_tier: Literal["silver"] = "silver"
    generator_version: Identifier
    generated_at: datetime
    generator_source_sha256: Sha256
    source_config_sha256: Sha256
    settings: CalibrationBuildSettings
    repositories: list[CalibrationRepositorySnapshot] = Field(min_length=4)
    artifacts: CalibrationArtifacts
    summary: CalibrationSummary
    label_policy: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    build_checks: dict[str, bool]

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_repositories(self) -> CalibrationDatasetManifest:
        _unique_by(self.repositories, key=lambda item: item.id, kind="repository")
        if not self.build_checks or not all(self.build_checks.values()):
            raise ValueError("all recorded build checks must pass")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CalibrationDatasetBundle:
    root: Path
    manifest: CalibrationDatasetManifest
    candidates: tuple[CalibrationCandidate, ...]
    queries: dict[CalibrationSplit, tuple[CalibrationQuery, ...]]
    judgments: dict[CalibrationSplit, tuple[CalibrationJudgment, ...]]

    @property
    def digest(self) -> str:
        return self.manifest.digest()


def load_calibration_source_config(path: Path) -> CalibrationSourceConfig:
    return _load_json_model(path, CalibrationSourceConfig)


def load_calibration_dataset(root: Path) -> CalibrationDatasetBundle:
    resolved_root = root.resolve(strict=True)
    manifest = _load_json_model(resolved_root / "manifest.json", CalibrationDatasetManifest)
    candidates = tuple(
        _load_jsonl(
            resolved_root,
            manifest.artifacts.candidates,
            CalibrationCandidate,
        )
    )
    queries: dict[CalibrationSplit, tuple[CalibrationQuery, ...]] = {}
    judgments: dict[CalibrationSplit, tuple[CalibrationJudgment, ...]] = {}
    for split in CalibrationSplit:
        artifacts = manifest.artifacts.for_split(split)
        queries[split] = tuple(_load_jsonl(resolved_root, artifacts.queries, CalibrationQuery))
        judgments[split] = tuple(_load_jsonl(resolved_root, artifacts.qrels, CalibrationJudgment))
    bundle = CalibrationDatasetBundle(
        root=resolved_root,
        manifest=manifest,
        candidates=candidates,
        queries=queries,
        judgments=judgments,
    )
    _validate_bundle(bundle)
    return bundle


def load_runtime_split(
    root: Path, split: CalibrationSplit
) -> tuple[
    CalibrationDatasetManifest,
    tuple[CalibrationCandidate, ...],
    tuple[CalibrationQuery, ...],
]:
    """Load runtime inputs without opening the split's qrels artifact."""

    resolved_root = root.resolve(strict=True)
    manifest = _load_json_model(resolved_root / "manifest.json", CalibrationDatasetManifest)
    candidates = tuple(
        _load_jsonl(resolved_root, manifest.artifacts.candidates, CalibrationCandidate)
    )
    queries = tuple(
        _load_jsonl(
            resolved_root,
            manifest.artifacts.for_split(split).queries,
            CalibrationQuery,
        )
    )
    candidate_ids = {candidate.id for candidate in candidates}
    for query in queries:
        if query.split is not split:
            raise ValueError(f"query {query.id} is stored in the wrong split")
        missing = set(query.candidate_ids) - candidate_ids
        if missing:
            raise ValueError(f"query {query.id} references missing candidates: {sorted(missing)}")
    return manifest, candidates, queries


def _load_json_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    raw = path.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON: {path}") from exc
    return TypeAdapter(model).validate_python(decoded)


def _artifact_path(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"artifact escapes dataset root: {relative}")
    return candidate


def _load_jsonl[ModelT: BaseModel](
    root: Path,
    metadata: ArtifactMetadata,
    model: type[ModelT],
) -> list[ModelT]:
    path = _artifact_path(root, metadata.path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != metadata.sha256:
        raise ValueError(f"artifact hash mismatch: {metadata.path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"artifact is not UTF-8: {metadata.path}") from exc
    lines = text.splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError(f"artifact contains blank JSONL rows: {metadata.path}")
    if len(lines) != metadata.rows:
        raise ValueError(f"artifact row count mismatch: {metadata.path}")
    adapter = TypeAdapter(model)
    values: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            values.append(adapter.validate_python(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row {metadata.path}:{line_number}") from exc
    return values


def _validate_bundle(bundle: CalibrationDatasetBundle) -> None:
    manifest = bundle.manifest
    repositories = _unique_by(manifest.repositories, key=lambda item: item.id, kind="repository")
    candidates = _unique_by(bundle.candidates, key=lambda item: item.id, kind="candidate")
    all_queries = [query for split in CalibrationSplit for query in bundle.queries[split]]
    queries = _unique_by(all_queries, key=lambda item: item.id, kind="query")
    all_judgments = [judgment for split in CalibrationSplit for judgment in bundle.judgments[split]]
    judgment_pairs = _unique_by(
        all_judgments,
        key=lambda item: f"{item.query_id}\x1f{item.candidate_id}",
        kind="judgment",
    )

    used_candidates: set[str] = set()
    for split in CalibrationSplit:
        split_queries = bundle.queries[split]
        split_judgments = bundle.judgments[split]
        by_query: dict[str, list[CalibrationJudgment]] = {}
        for judgment in split_judgments:
            by_query.setdefault(judgment.query_id, []).append(judgment)
        for query in split_queries:
            repository = repositories.get(query.repository_id)
            if repository is None or repository.split is not split:
                raise ValueError(f"query {query.id} violates repository-held-out split")
            expected_ids = set(query.candidate_ids)
            observed = by_query.get(query.id, [])
            observed_ids = {item.candidate_id for item in observed}
            if expected_ids != observed_ids:
                raise ValueError(f"query {query.id} qrels do not match its candidate pool")
            target_commits = {item.evidence.target_commit for item in observed}
            if len(target_commits) != 1:
                raise ValueError(f"query {query.id} judgments disagree on target commit")
            target_commit = next(iter(target_commits))
            runtime_query = json.dumps(
                query.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            ).lower()
            if target_commit in runtime_query:
                raise ValueError(f"query {query.id} leaks its target commit")
            if not any(item.required for item in observed):
                raise ValueError(f"query {query.id} has no exact-path required candidate")
            eligibility = {item.eligibility for item in observed}
            if CandidateEligibility.FUTURE_GUARD not in eligibility:
                raise ValueError(f"query {query.id} has no future guard")
            if CandidateEligibility.CROSS_SCOPE_GUARD not in eligibility:
                raise ValueError(f"query {query.id} has no cross-scope guard")
            used_candidates.update(expected_ids)
            for judgment in observed:
                candidate = candidates.get(judgment.candidate_id)
                if candidate is None:
                    raise ValueError(
                        f"judgment references unknown candidate {judgment.candidate_id}"
                    )
                if candidate.source_commit == judgment.evidence.target_commit:
                    raise ValueError(f"query {query.id} leaked its target commit as a candidate")
                runtime_candidate = json.dumps(
                    candidate.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ).lower()
                if target_commit in runtime_candidate:
                    raise ValueError(
                        f"query {query.id} target commit appears in a runtime candidate"
                    )
                if judgment.eligibility is CandidateEligibility.CROSS_SCOPE_GUARD:
                    if candidate.repository_id == query.repository_id:
                        raise ValueError(f"query {query.id} cross-scope guard is in-scope")
                elif candidate.repository_id != query.repository_id:
                    raise ValueError(f"query {query.id} eligible/future candidate is out-of-scope")

    if set(candidates) != used_candidates:
        unused = sorted(set(candidates) - used_candidates)
        raise ValueError(f"dataset contains unused candidates: {unused[:10]}")
    if set(queries) != {item.query_id for item in all_judgments}:
        raise ValueError("query and judgment references do not match")
    if len(judgment_pairs) != len(all_judgments):
        raise AssertionError("duplicate judgment validation failed")

    actual_queries_by_split = {
        split.value: len(bundle.queries[split]) for split in CalibrationSplit
    }
    if actual_queries_by_split != manifest.summary.queries_by_split:
        raise ValueError("manifest queries_by_split does not match artifacts")
    if len(candidates) != manifest.summary.candidates:
        raise ValueError("manifest candidate count does not match artifacts")
    if len(queries) != manifest.summary.queries:
        raise ValueError("manifest query count does not match artifacts")
    if len(all_judgments) != manifest.summary.judgments:
        raise ValueError("manifest judgment count does not match artifacts")


def _unique_by[ItemT](
    items: list[ItemT] | tuple[ItemT, ...],
    *,
    key: Callable[[ItemT], object],
    kind: str,
) -> dict[str, ItemT]:
    indexed: dict[str, ItemT] = {}
    for item in items:
        identity = str(key(item))
        if identity in indexed:
            raise ValueError(f"duplicate {kind}: {identity}")
        indexed[identity] = item
    return indexed


__all__ = [
    "ArtifactMetadata",
    "CalibrationArtifacts",
    "CalibrationBuildSettings",
    "CalibrationCandidate",
    "CalibrationDatasetBundle",
    "CalibrationDatasetManifest",
    "CalibrationJudgment",
    "CalibrationQuery",
    "CalibrationRepositorySnapshot",
    "CalibrationRepositorySource",
    "CalibrationSourceConfig",
    "CalibrationSplit",
    "CalibrationSummary",
    "CandidateEligibility",
    "JudgmentOrigin",
    "RepositoryRole",
    "SilverJudgmentEvidence",
    "SplitArtifacts",
    "load_calibration_dataset",
    "load_calibration_source_config",
    "load_runtime_split",
]
