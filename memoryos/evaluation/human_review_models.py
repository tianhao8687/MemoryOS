from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN.pattern)]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN.pattern)]


def _default_review_splits() -> list[Literal["train", "dev"]]:
    return ["train", "dev"]


class ReviewPartition(StrEnum):
    CALIBRATION = "calibration"
    VALIDATION = "validation"
    DIAGNOSTIC = "diagnostic"


class ReviewSourceKind(StrEnum):
    GIT_HISTORY = "git_history"
    REAL_WORKLOAD = "real_workload"


class SafetyDisposition(StrEnum):
    ALLOW = "allow"
    EXCLUDE = "exclude"
    UNCERTAIN = "uncertain"


class ReviewerConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewIssueTag(StrEnum):
    FUTURE_INFORMATION = "future_information"
    WRONG_SCOPE = "wrong_scope"
    STALE = "stale"
    CONTRADICTORY = "contradictory"
    MISLEADING = "misleading"
    DUPLICATE = "duplicate"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    OTHER = "other"


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


class GitRuntimeReviewSource(StrictModel):
    dataset_root: str = Field(min_length=1, max_length=500)
    samples_per_repository: int = Field(default=12, ge=1, le=100)
    splits: list[Literal["train", "dev"]] = Field(
        default_factory=_default_review_splits, min_length=1, max_length=2
    )

    @field_validator("dataset_root")
    @classmethod
    def validate_dataset_root(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="dataset_root")

    @field_validator("splits")
    @classmethod
    def validate_splits(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("git source splits must be unique")
        return value


class RealWorkloadReviewSource(StrictModel):
    manifest_path: str = Field(min_length=1, max_length=500)

    @field_validator("manifest_path")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="manifest_path")


class HumanReviewSourceConfig(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: Identifier
    generated_at: datetime
    sampling_seed: Identifier
    rubric_version: Identifier = "retrieval-utility-v1"
    assignment_ids: list[Identifier] = Field(min_length=2, max_length=10)
    git_sources: list[GitRuntimeReviewSource] = Field(min_length=1, max_length=20)
    real_workload_sources: list[RealWorkloadReviewSource] = Field(
        default_factory=list, max_length=100
    )

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_unique_inputs(self) -> HumanReviewSourceConfig:
        if len(set(self.assignment_ids)) != len(self.assignment_ids):
            raise ValueError("assignment_ids must be unique")
        git_paths = [item.dataset_root for item in self.git_sources]
        if len(set(git_paths)) != len(git_paths):
            raise ValueError("git dataset roots must be unique")
        workload_paths = [item.manifest_path for item in self.real_workload_sources]
        if len(set(workload_paths)) != len(workload_paths):
            raise ValueError("real workload manifest paths must be unique")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class BlindReviewCandidate(StrictModel):
    id: Identifier
    repository_id: Identifier
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    category: str | None = Field(default=None, min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=20_000)
    source_paths: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="observed_at")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def validate_optional_time(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _require_aware(value, field_name=info.field_name)

    @field_validator("source_paths")
    @classmethod
    def validate_source_paths(cls, value: list[str]) -> list[str]:
        normalized = [_validate_relative_path(path, field_name="source_paths") for path in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_paths must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_validity_window(self) -> BlindReviewCandidate:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class BlindReviewCase(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: Identifier
    id: Identifier
    partition: ReviewPartition
    source_kind: ReviewSourceKind
    query: str = Field(min_length=1, max_length=20_000)
    query_repository_id: Identifier
    cutoff_time: datetime
    candidates: list[BlindReviewCandidate] = Field(min_length=1, max_length=500)
    rubric_version: Identifier

    @field_validator("cutoff_time")
    @classmethod
    def validate_cutoff_time(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="cutoff_time")

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> BlindReviewCase:
        identities = [candidate.id for candidate in self.candidates]
        if len(set(identities)) != len(identities):
            raise ValueError("candidate ids must be unique within a review case")
        return self


class ReviewResponseDraft(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: Identifier
    case_id: Identifier
    candidate_id: Identifier
    reviewer_id: Identifier | None = None
    semantic_relevance: int | None = Field(default=None, ge=0, le=3)
    safety_disposition: SafetyDisposition | None = None
    must_retrieve: bool | None = None
    reviewer_confidence: ReviewerConfidence | None = None
    issue_tags: list[ReviewIssueTag] = Field(default_factory=list, max_length=8)
    rationale: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_draft_consistency(self) -> ReviewResponseDraft:
        if len(set(self.issue_tags)) != len(self.issue_tags):
            raise ValueError("issue_tags must be unique")
        decision_fields = (
            self.reviewer_id,
            self.semantic_relevance,
            self.safety_disposition,
            self.must_retrieve,
            self.reviewer_confidence,
        )
        populated = [value is not None for value in decision_fields]
        if any(populated) and not all(populated):
            raise ValueError("a started response must complete every required decision field")
        if not all(populated):
            if self.issue_tags or self.rationale is not None:
                raise ValueError("blank response templates cannot contain annotations")
            return self
        if self.must_retrieve and (
            self.semantic_relevance is None
            or self.semantic_relevance < 2
            or self.safety_disposition is not SafetyDisposition.ALLOW
        ):
            raise ValueError("must_retrieve requires relevance>=2 and safety_disposition=allow")
        if self.safety_disposition is SafetyDisposition.EXCLUDE and self.must_retrieve:
            raise ValueError("excluded candidates cannot be required")
        rationale_required = (
            self.semantic_relevance == 3
            or bool(self.must_retrieve)
            or self.safety_disposition is not SafetyDisposition.ALLOW
        )
        if rationale_required and self.rationale is None:
            raise ValueError("high-impact, required, or unsafe decisions require a rationale")
        return self

    @property
    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.reviewer_id,
                self.semantic_relevance,
                self.safety_disposition,
                self.must_retrieve,
                self.reviewer_confidence,
            )
        )


class AdjudicatedReview(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: Identifier
    candidate_id: Identifier
    adjudicator_id: Identifier
    reviewer_ids: list[Identifier] = Field(min_length=2, max_length=10)
    semantic_relevance: int = Field(ge=0, le=3)
    safety_disposition: SafetyDisposition
    must_retrieve: bool
    issue_tags: list[ReviewIssueTag] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_adjudication(self) -> AdjudicatedReview:
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError("reviewer_ids must be unique")
        if self.adjudicator_id in self.reviewer_ids:
            raise ValueError("adjudicator must be independent from the listed reviewers")
        if len(set(self.issue_tags)) != len(self.issue_tags):
            raise ValueError("issue_tags must be unique")
        if self.must_retrieve and (
            self.semantic_relevance < 2 or self.safety_disposition is not SafetyDisposition.ALLOW
        ):
            raise ValueError("must_retrieve requires relevance>=2 and safety_disposition=allow")
        return self


class ReviewSourceSnapshot(StrictModel):
    id: Identifier
    kind: ReviewSourceKind
    path: str = Field(min_length=1, max_length=500)
    source_manifest_sha256: Sha256
    repositories: list[Identifier] = Field(min_length=1, max_length=500)
    cases: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="source path")

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source repositories must be unique")
        return value


class ReviewArtifactMetadata(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: Sha256
    rows: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="artifact path")


class HumanReviewArtifacts(StrictModel):
    assignments: dict[Identifier, ReviewArtifactMetadata]
    response_templates: dict[Identifier, ReviewArtifactMetadata]
    source_map: ReviewArtifactMetadata
    coupling_audit: ReviewArtifactMetadata
    rubric: ReviewArtifactMetadata

    @model_validator(mode="after")
    def validate_assignment_artifacts(self) -> HumanReviewArtifacts:
        if set(self.assignments) != set(self.response_templates):
            raise ValueError("assignments and response templates must have identical ids")
        return self


class HumanReviewSummary(StrictModel):
    cases: int = Field(ge=1)
    judgments_per_reviewer: int = Field(ge=1)
    cases_by_partition: dict[str, int]
    cases_by_source_kind: dict[str, int]
    cases_by_repository: dict[str, int]
    source_datasets: int = Field(ge=1)
    source_repositories: int = Field(ge=1)
    assignment_count: int = Field(ge=2)


class HumanReviewPackManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: Identifier
    label_tier: Literal["pending_human_adjudication"] = "pending_human_adjudication"
    status: Literal["pilot_unlabeled"] = "pilot_unlabeled"
    generated_at: datetime
    rubric_version: Identifier
    source_config_sha256: Sha256
    generator_source_sha256: Sha256
    qrels_loaded_during_build: Literal[False] = False
    test_split_sealed: Literal[True] = True
    sources: list[ReviewSourceSnapshot] = Field(min_length=1)
    artifacts: HumanReviewArtifacts
    summary: HumanReviewSummary
    build_checks: dict[str, bool]
    limitations: list[str] = Field(min_length=1)
    release_blockers: list[str] = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware(value, field_name="generated_at")

    @model_validator(mode="after")
    def validate_manifest(self) -> HumanReviewPackManifest:
        source_ids = [source.id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("review source ids must be unique")
        if not self.build_checks or not all(self.build_checks.values()):
            raise ValueError("all human-review build checks must pass")
        if self.summary.source_datasets != len(self.sources):
            raise ValueError("source dataset summary does not match sources")
        if self.summary.assignment_count != len(self.artifacts.assignments):
            raise ValueError("assignment summary does not match artifacts")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ReviewSourceMapRow(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: Identifier
    candidate_id: Identifier
    source_kind: ReviewSourceKind
    source_dataset_id: Identifier
    source_query_id: Identifier
    source_candidate_id: Identifier
    source_partition: Literal["train", "dev", "diagnostic"]
    source_repository_id: Identifier


@dataclass(frozen=True)
class HumanReviewPackBundle:
    root: Path
    manifest: HumanReviewPackManifest
    assignments: dict[str, tuple[BlindReviewCase, ...]]
    response_templates: dict[str, tuple[ReviewResponseDraft, ...]]
    source_map: tuple[ReviewSourceMapRow, ...]
    coupling_audit: dict[str, Any]

    @property
    def digest(self) -> str:
        return self.manifest.digest()


def load_human_review_source_config(path: Path) -> HumanReviewSourceConfig:
    return _load_json_model(path, HumanReviewSourceConfig)


def load_human_review_pack(root: Path) -> HumanReviewPackBundle:
    resolved_root = root.resolve(strict=True)
    manifest = _load_json_model(resolved_root / "manifest.json", HumanReviewPackManifest)
    assignments = {
        assignment_id: tuple(_load_jsonl(resolved_root, metadata, BlindReviewCase))
        for assignment_id, metadata in manifest.artifacts.assignments.items()
    }
    templates = {
        assignment_id: tuple(_load_jsonl(resolved_root, metadata, ReviewResponseDraft))
        for assignment_id, metadata in manifest.artifacts.response_templates.items()
    }
    source_map = tuple(
        _load_jsonl(resolved_root, manifest.artifacts.source_map, ReviewSourceMapRow)
    )
    coupling_audit = _load_json_artifact(resolved_root, manifest.artifacts.coupling_audit)
    _verify_artifact(resolved_root, manifest.artifacts.rubric)
    bundle = HumanReviewPackBundle(
        root=resolved_root,
        manifest=manifest,
        assignments=assignments,
        response_templates=templates,
        source_map=source_map,
        coupling_audit=coupling_audit,
    )
    _validate_pack(bundle)
    return bundle


def load_completed_review(
    bundle: HumanReviewPackBundle, path: Path
) -> tuple[ReviewResponseDraft, ...]:
    rows = _load_external_jsonl(path, ReviewResponseDraft)
    if not rows:
        raise ValueError("completed review cannot be empty")
    assignment_ids = {row.assignment_id for row in rows}
    if len(assignment_ids) != 1:
        raise ValueError("completed review must contain exactly one assignment_id")
    assignment_id = next(iter(assignment_ids))
    if assignment_id not in bundle.assignments:
        raise ValueError(f"unknown assignment_id: {assignment_id}")
    if not all(row.is_complete for row in rows):
        raise ValueError("completed review contains blank decisions")
    reviewer_ids = {row.reviewer_id for row in rows}
    if len(reviewer_ids) != 1 or None in reviewer_ids:
        raise ValueError("completed review must use one non-empty reviewer_id")
    observed_pairs = {(row.case_id, row.candidate_id) for row in rows}
    expected_pairs = _assignment_pairs(bundle.assignments[assignment_id])
    if len(observed_pairs) != len(rows):
        raise ValueError("completed review contains duplicate case/candidate rows")
    if observed_pairs != expected_pairs:
        raise ValueError("completed review does not cover the assigned case/candidate pairs")
    return tuple(rows)


def load_adjudication(
    bundle: HumanReviewPackBundle,
    paths: list[Path],
    adjudication_path: Path,
) -> tuple[AdjudicatedReview, ...]:
    if len(paths) < 2:
        raise ValueError("adjudication requires at least two completed reviews")
    reviews = [load_completed_review(bundle, path) for path in paths]
    reviewer_ids = {str(review[0].reviewer_id) for review in reviews}
    assignment_ids = {review[0].assignment_id for review in reviews}
    if len(reviewer_ids) != len(reviews):
        raise ValueError("independent completed reviews require distinct reviewer ids")
    if len(assignment_ids) != len(reviews):
        raise ValueError("independent reviews must use distinct assignments")
    rows = _load_external_jsonl(adjudication_path, AdjudicatedReview)
    expected_pairs = _assignment_pairs(next(iter(bundle.assignments.values())))
    observed_pairs = {(row.case_id, row.candidate_id) for row in rows}
    if len(observed_pairs) != len(rows):
        raise ValueError("adjudication contains duplicate case/candidate rows")
    if observed_pairs != expected_pairs:
        raise ValueError("adjudication does not cover every review pair")
    if any(set(row.reviewer_ids) != reviewer_ids for row in rows):
        raise ValueError("adjudication reviewer_ids do not match completed reviews")
    return tuple(rows)


def review_agreement(
    left: tuple[ReviewResponseDraft, ...],
    right: tuple[ReviewResponseDraft, ...],
) -> dict[str, int | float]:
    left_index = {(row.case_id, row.candidate_id): row for row in left}
    right_index = {(row.case_id, row.candidate_id): row for row in right}
    if set(left_index) != set(right_index):
        raise ValueError("reviews do not cover the same case/candidate pairs")
    total = len(left_index)
    relevance_agreements = sum(
        left_index[pair].semantic_relevance == right_index[pair].semantic_relevance
        for pair in left_index
    )
    safety_agreements = sum(
        left_index[pair].safety_disposition == right_index[pair].safety_disposition
        for pair in left_index
    )
    full_agreements = sum(
        (
            left_index[pair].semantic_relevance == right_index[pair].semantic_relevance
            and left_index[pair].safety_disposition == right_index[pair].safety_disposition
            and left_index[pair].must_retrieve == right_index[pair].must_retrieve
        )
        for pair in left_index
    )
    return {
        "pairs": total,
        "relevance_exact_agreements": relevance_agreements,
        "relevance_exact_rate": relevance_agreements / total,
        "safety_exact_agreements": safety_agreements,
        "safety_exact_rate": safety_agreements / total,
        "full_decision_agreements": full_agreements,
        "full_decision_rate": full_agreements / total,
    }


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
        raise ValueError(f"artifact escapes review-pack root: {relative}")
    return candidate


def _verify_artifact(root: Path, metadata: ReviewArtifactMetadata) -> bytes:
    path = _artifact_path(root, metadata.path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata.sha256:
        raise ValueError(f"artifact hash mismatch: {metadata.path}")
    return raw


def _load_jsonl[ModelT: BaseModel](
    root: Path,
    metadata: ReviewArtifactMetadata,
    model: type[ModelT],
) -> list[ModelT]:
    raw = _verify_artifact(root, metadata)
    rows = _decode_jsonl(raw, model, label=metadata.path)
    if len(rows) != metadata.rows:
        raise ValueError(f"artifact row count mismatch: {metadata.path}")
    return rows


def _load_external_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    return _decode_jsonl(path.read_bytes(), model, label=str(path))


def _decode_jsonl[ModelT: BaseModel](
    raw: bytes, model: type[ModelT], *, label: str
) -> list[ModelT]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"artifact is not UTF-8: {label}") from exc
    lines = text.splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError(f"artifact contains blank JSONL rows: {label}")
    adapter = TypeAdapter(model)
    values: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            values.append(adapter.validate_python(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row {label}:{line_number}") from exc
    return values


def _load_json_artifact(root: Path, metadata: ReviewArtifactMetadata) -> dict[str, Any]:
    raw = _verify_artifact(root, metadata)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {metadata.path}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"JSON artifact must contain an object: {metadata.path}")
    if metadata.rows != 1:
        raise ValueError(f"object artifact must record one row: {metadata.path}")
    return decoded


def _assignment_pairs(cases: tuple[BlindReviewCase, ...]) -> set[tuple[str, str]]:
    return {(case.id, candidate.id) for case in cases for candidate in case.candidates}


def _validate_pack(bundle: HumanReviewPackBundle) -> None:
    manifest = bundle.manifest
    assignment_ids = set(bundle.assignments)
    if assignment_ids != set(manifest.artifacts.assignments):
        raise ValueError("loaded assignment ids differ from manifest")
    expected_case_ids: set[str] | None = None
    expected_pairs: set[tuple[str, str]] | None = None
    case_orders: dict[str, list[tuple[str, ...]]] = {}
    for assignment_id, cases in bundle.assignments.items():
        if any(case.assignment_id != assignment_id for case in cases):
            raise ValueError(f"assignment file contains another assignment id: {assignment_id}")
        case_ids = {case.id for case in cases}
        if len(case_ids) != len(cases):
            raise ValueError(f"assignment contains duplicate review cases: {assignment_id}")
        pairs = _assignment_pairs(cases)
        if expected_case_ids is None:
            expected_case_ids = case_ids
            expected_pairs = pairs
        elif case_ids != expected_case_ids or pairs != expected_pairs:
            raise ValueError("all assignments must cover identical cases and candidates")
        for case in cases:
            case_orders.setdefault(case.id, []).append(
                tuple(candidate.id for candidate in case.candidates)
            )
            _reject_blind_label_leak(case.model_dump(mode="json"))
        templates = bundle.response_templates[assignment_id]
        if any(row.is_complete for row in templates):
            raise ValueError("checked-in response templates must remain blank")
        template_pairs = {(row.case_id, row.candidate_id) for row in templates}
        if len(template_pairs) != len(templates) or template_pairs != pairs:
            raise ValueError("response template does not match its assignment")
        if any(row.assignment_id != assignment_id for row in templates):
            raise ValueError("response template contains another assignment id")
    assert expected_case_ids is not None
    assert expected_pairs is not None
    source_pairs = {(row.case_id, row.candidate_id) for row in bundle.source_map}
    if len(source_pairs) != len(bundle.source_map) or source_pairs != expected_pairs:
        raise ValueError("control source map must cover each blind review pair exactly once")
    if len(bundle.assignments) > 1 and not any(
        len(set(orders)) > 1 for orders in case_orders.values()
    ):
        raise ValueError("review assignments must use independent candidate ordering")
    observed_partition_counts: dict[str, int] = {}
    observed_source_counts: dict[str, int] = {}
    observed_repository_counts: dict[str, int] = {}
    first_assignment = next(iter(bundle.assignments.values()))
    for case in first_assignment:
        observed_partition_counts[case.partition.value] = (
            observed_partition_counts.get(case.partition.value, 0) + 1
        )
        observed_source_counts[case.source_kind.value] = (
            observed_source_counts.get(case.source_kind.value, 0) + 1
        )
        observed_repository_counts[case.query_repository_id] = (
            observed_repository_counts.get(case.query_repository_id, 0) + 1
        )
    summary = manifest.summary
    if (
        len(first_assignment) != summary.cases
        or len(expected_pairs) != summary.judgments_per_reviewer
    ):
        raise ValueError("case or judgment summary does not match assignments")
    if observed_partition_counts != summary.cases_by_partition:
        raise ValueError("partition summary does not match assignments")
    if observed_source_counts != summary.cases_by_source_kind:
        raise ValueError("source-kind summary does not match assignments")
    if observed_repository_counts != summary.cases_by_repository:
        raise ValueError("repository summary does not match assignments")


_FORBIDDEN_BLIND_KEYS = {
    "canary",
    "challenge_tags",
    "confidence",
    "cutoff_commit",
    "eligibility",
    "expectation",
    "forbidden",
    "importance",
    "relevance",
    "required",
    "solution_commit",
    "source_commit",
    "target_commit",
    "target_paths",
}


def _reject_blind_label_leak(value: Any) -> None:
    if isinstance(value, dict):
        leaked = _FORBIDDEN_BLIND_KEYS.intersection(value)
        if leaked:
            raise ValueError(f"blind assignment leaked scorer/control fields: {sorted(leaked)}")
        for nested in value.values():
            _reject_blind_label_leak(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_blind_label_leak(nested)


__all__ = [
    "AdjudicatedReview",
    "BlindReviewCandidate",
    "BlindReviewCase",
    "GitRuntimeReviewSource",
    "HumanReviewArtifacts",
    "HumanReviewPackBundle",
    "HumanReviewPackManifest",
    "HumanReviewSourceConfig",
    "HumanReviewSummary",
    "RealWorkloadReviewSource",
    "ReviewArtifactMetadata",
    "ReviewIssueTag",
    "ReviewPartition",
    "ReviewResponseDraft",
    "ReviewSourceKind",
    "ReviewSourceMapRow",
    "ReviewSourceSnapshot",
    "ReviewerConfidence",
    "SafetyDisposition",
    "load_adjudication",
    "load_completed_review",
    "load_human_review_pack",
    "load_human_review_source_config",
    "review_agreement",
]
