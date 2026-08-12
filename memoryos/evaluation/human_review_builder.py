from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from memoryos.evaluation.calibration_models import (
    CalibrationCandidate,
    CalibrationQuery,
    CalibrationSplit,
    load_runtime_split,
)
from memoryos.evaluation.human_review_audit import build_coupling_audit
from memoryos.evaluation.human_review_models import (
    BlindReviewCandidate,
    BlindReviewCase,
    HumanReviewArtifacts,
    HumanReviewPackManifest,
    HumanReviewSourceConfig,
    HumanReviewSummary,
    ReviewArtifactMetadata,
    ReviewPartition,
    ReviewResponseDraft,
    ReviewSourceKind,
    ReviewSourceMapRow,
    ReviewSourceSnapshot,
    load_human_review_pack,
)
from memoryos.evaluation.real_workload_models import load_real_workload_manifest


class HumanReviewBuildError(RuntimeError):
    """Raised when runtime-only sources cannot satisfy the blind review contract."""


@dataclass(frozen=True)
class _CaseMaterial:
    id: str
    partition: ReviewPartition
    source_kind: ReviewSourceKind
    query: str
    query_repository_id: str
    cutoff_time: datetime
    candidates: tuple[BlindReviewCandidate, ...]


class HumanReviewPackBuilder:
    def build(
        self,
        config: HumanReviewSourceConfig,
        *,
        repository_root: Path,
        rubric_path: Path,
        output_root: Path,
    ) -> HumanReviewPackManifest:
        resolved_repository_root = repository_root.resolve(strict=True)
        resolved_rubric = rubric_path.resolve(strict=True)
        if resolved_repository_root not in resolved_rubric.parents:
            raise HumanReviewBuildError("rubric must be inside the repository root")
        rubric_bytes = _normalized_text_bytes(resolved_rubric)

        cases: list[_CaseMaterial] = []
        source_map: list[ReviewSourceMapRow] = []
        snapshots: list[ReviewSourceSnapshot] = []
        source_ids: set[str] = set()

        for git_source in config.git_sources:
            dataset_root = _resolve_repository_path(
                resolved_repository_root, git_source.dataset_root, expect_directory=True
            )
            source_cases, source_rows, snapshot = self._git_source(
                config=config,
                dataset_root=dataset_root,
                source_path=git_source.dataset_root,
                samples_per_repository=git_source.samples_per_repository,
                split_names=git_source.splits,
            )
            _reserve_source_id(snapshot.id, source_ids)
            cases.extend(source_cases)
            source_map.extend(source_rows)
            snapshots.append(snapshot)

        for workload_source in config.real_workload_sources:
            manifest_path = _resolve_repository_path(
                resolved_repository_root,
                workload_source.manifest_path,
                expect_directory=False,
            )
            source_cases, source_rows, snapshot = self._real_workload_source(
                config=config,
                manifest_path=manifest_path,
                source_path=workload_source.manifest_path,
            )
            _reserve_source_id(snapshot.id, source_ids)
            cases.extend(source_cases)
            source_map.extend(source_rows)
            snapshots.append(snapshot)

        if not cases:
            raise HumanReviewBuildError("review pack requires at least one case")
        case_ids = [case.id for case in cases]
        if len(set(case_ids)) != len(case_ids):
            raise HumanReviewBuildError("review case ids must be globally unique")
        pair_count = sum(len(case.candidates) for case in cases)
        if pair_count != len(source_map):
            raise HumanReviewBuildError("source map does not match review candidate pairs")

        partition_counts = Counter(case.partition.value for case in cases)
        source_kind_counts = Counter(case.source_kind.value for case in cases)
        repository_counts = Counter(case.query_repository_id for case in cases)
        source_repositories = {
            repository for snapshot in snapshots for repository in snapshot.repositories
        }
        summary = HumanReviewSummary(
            cases=len(cases),
            judgments_per_reviewer=pair_count,
            cases_by_partition=dict(sorted(partition_counts.items())),
            cases_by_source_kind=dict(sorted(source_kind_counts.items())),
            cases_by_repository=dict(sorted(repository_counts.items())),
            source_datasets=len(snapshots),
            source_repositories=len(source_repositories),
            assignment_count=len(config.assignment_ids),
        )
        audit = build_coupling_audit(
            dataset_id=config.dataset_id,
            sources=snapshots,
            cases_by_source_kind=summary.cases_by_source_kind,
            total_cases=summary.cases,
        )

        resolved_output = output_root.resolve()
        resolved_output.mkdir(parents=True, exist_ok=True)
        assignments: dict[str, ReviewArtifactMetadata] = {}
        templates: dict[str, ReviewArtifactMetadata] = {}
        primary_candidate_orders: dict[str, tuple[str, ...]] = {}
        for assignment_index, assignment_id in enumerate(config.assignment_ids):
            assignment_cases = self._assignment_cases(
                config=config,
                assignment_id=assignment_id,
                assignment_index=assignment_index,
                cases=cases,
                primary_candidate_orders=primary_candidate_orders,
            )
            assignment_path = f"blind/{assignment_id}.jsonl"
            assignments[assignment_id] = _write_jsonl(
                resolved_output, assignment_path, assignment_cases
            )
            response_rows = [
                ReviewResponseDraft(
                    assignment_id=assignment_id,
                    case_id=case.id,
                    candidate_id=candidate.id,
                )
                for case in assignment_cases
                for candidate in case.candidates
            ]
            template_path = f"templates/{assignment_id}.responses.jsonl"
            templates[assignment_id] = _write_jsonl(
                resolved_output, template_path, response_rows, exclude_none=False
            )

        ordered_source_map = sorted(source_map, key=lambda row: (row.case_id, row.candidate_id))
        source_map_metadata = _write_jsonl(
            resolved_output,
            "control/source_map.jsonl",
            ordered_source_map,
        )
        audit_metadata = _write_json_bytes(
            resolved_output, "coupling_audit.json", audit.canonical_json().encode("utf-8")
        )
        rubric_metadata = _write_bytes(resolved_output, "RUBRIC.md", rubric_bytes, rows=1)
        artifacts = HumanReviewArtifacts(
            assignments=assignments,
            response_templates=templates,
            source_map=source_map_metadata,
            coupling_audit=audit_metadata,
            rubric=rubric_metadata,
        )
        manifest = HumanReviewPackManifest(
            dataset_id=config.dataset_id,
            generated_at=config.generated_at,
            rubric_version=config.rubric_version,
            source_config_sha256=config.digest(),
            generator_source_sha256=generator_source_digest(),
            sources=snapshots,
            artifacts=artifacts,
            summary=summary,
            build_checks={
                "blind_assignments_contain_no_label_or_control_fields": True,
                "candidate_order_differs_by_assignment": True,
                "control_source_map_is_separate": True,
                "qrels_were_not_loaded": True,
                "response_templates_are_blank": True,
                "source_artifacts_are_hash_pinned": True,
                "test_split_is_absent": True,
                "time_stratified_repository_sampling": True,
                "two_or_more_independent_assignments": True,
            },
            limitations=[
                "This is an unlabeled pilot review pack, not a completed human-gold dataset.",
                "Git-history cases inherit candidate construction from the silver dataset even "
                "though silver qrels are never loaded.",
                "The initial real-workload source is diagnostic and may overlap a Git source "
                "repository.",
                "Human relevance labels alone cannot calibrate truth mutation, conflict "
                "confidence, or memory-health automation.",
            ],
            release_blockers=audit.release_blockers,
        )
        _write_manifest(resolved_output / "manifest.json", manifest)
        loaded = load_human_review_pack(resolved_output)
        if loaded.digest != manifest.digest():
            raise HumanReviewBuildError("written review pack differs from in-memory manifest")
        return manifest

    def _git_source(
        self,
        *,
        config: HumanReviewSourceConfig,
        dataset_root: Path,
        source_path: str,
        samples_per_repository: int,
        split_names: Sequence[str],
    ) -> tuple[list[_CaseMaterial], list[ReviewSourceMapRow], ReviewSourceSnapshot]:
        cases: list[_CaseMaterial] = []
        source_rows: list[ReviewSourceMapRow] = []
        source_manifest = None
        selected_repositories: set[str] = set()
        for split_name in split_names:
            split = CalibrationSplit(split_name)
            manifest, candidates, queries = load_runtime_split(dataset_root, split)
            if source_manifest is None:
                source_manifest = manifest
            elif source_manifest.digest() != manifest.digest():
                raise HumanReviewBuildError("runtime splits came from different manifests")
            candidate_index = {candidate.id: candidate for candidate in candidates}
            by_repository: defaultdict[str, list[CalibrationQuery]] = defaultdict(list)
            for query in queries:
                by_repository[query.repository_id].append(query)
            partition = (
                ReviewPartition.CALIBRATION
                if split is CalibrationSplit.TRAIN
                else ReviewPartition.VALIDATION
            )
            for repository_id, repository_queries in sorted(by_repository.items()):
                selected_repositories.add(repository_id)
                selected = _time_stratified_sample(
                    repository_queries,
                    maximum=samples_per_repository,
                    seed=f"{config.sampling_seed}:{manifest.dataset_id}:{repository_id}:{split.value}",
                )
                for query in selected:
                    case_id = _opaque_id("case", config.dataset_id, manifest.dataset_id, query.id)
                    blind_candidates: list[BlindReviewCandidate] = []
                    for candidate_id in query.candidate_ids:
                        source_candidate = candidate_index.get(candidate_id)
                        if source_candidate is None:
                            raise HumanReviewBuildError(
                                f"query {query.id} references missing runtime candidate "
                                f"{candidate_id}"
                            )
                        blind_candidate = _git_candidate(
                            config.dataset_id, case_id, source_candidate
                        )
                        blind_candidates.append(blind_candidate)
                        source_rows.append(
                            ReviewSourceMapRow(
                                case_id=case_id,
                                candidate_id=blind_candidate.id,
                                source_kind=ReviewSourceKind.GIT_HISTORY,
                                source_dataset_id=manifest.dataset_id,
                                source_query_id=query.id,
                                source_candidate_id=source_candidate.id,
                                source_partition=split.value,
                                source_repository_id=source_candidate.repository_id,
                            )
                        )
                    cases.append(
                        _CaseMaterial(
                            id=case_id,
                            partition=partition,
                            source_kind=ReviewSourceKind.GIT_HISTORY,
                            query=query.query,
                            query_repository_id=query.repository_id,
                            cutoff_time=query.cutoff_time,
                            candidates=tuple(blind_candidates),
                        )
                    )
        if source_manifest is None:
            raise HumanReviewBuildError("git runtime source contains no requested split")
        if not cases:
            raise HumanReviewBuildError(f"git runtime source produced no cases: {source_path}")
        snapshot = ReviewSourceSnapshot(
            id=source_manifest.dataset_id,
            kind=ReviewSourceKind.GIT_HISTORY,
            path=source_path,
            source_manifest_sha256=source_manifest.digest(),
            repositories=sorted(selected_repositories),
            cases=len(cases),
        )
        return cases, source_rows, snapshot

    def _real_workload_source(
        self,
        *,
        config: HumanReviewSourceConfig,
        manifest_path: Path,
        source_path: str,
    ) -> tuple[list[_CaseMaterial], list[ReviewSourceMapRow], ReviewSourceSnapshot]:
        manifest = load_real_workload_manifest(manifest_path)
        memories = {memory.id: memory for memory in manifest.memories}
        cases: list[_CaseMaterial] = []
        source_rows: list[ReviewSourceMapRow] = []
        selected_repositories: set[str] = set()
        source_namespace = f"{manifest.name}:{source_path}"
        for task in manifest.tasks:
            if not task.memory_seed_ids:
                raise HumanReviewBuildError(
                    f"real workload task has no memory candidates: {task.id}"
                )
            selected_repositories.add(task.repository_id)
            case_id = _opaque_id("case", config.dataset_id, source_namespace, task.id)
            candidates: list[BlindReviewCandidate] = []
            for memory_id in task.memory_seed_ids:
                memory = memories[memory_id]
                candidate_id = _opaque_id("candidate", config.dataset_id, case_id, memory.id)
                candidate = BlindReviewCandidate(
                    id=candidate_id,
                    repository_id=memory.repository_id,
                    observed_at=memory.captured_at,
                    valid_from=memory.valid_from,
                    valid_to=memory.valid_to,
                    category=memory.category,
                    title=memory.title,
                    content=memory.content,
                )
                candidates.append(candidate)
                source_rows.append(
                    ReviewSourceMapRow(
                        case_id=case_id,
                        candidate_id=candidate_id,
                        source_kind=ReviewSourceKind.REAL_WORKLOAD,
                        source_dataset_id=manifest.name,
                        source_query_id=task.id,
                        source_candidate_id=memory.id,
                        source_partition="diagnostic",
                        source_repository_id=memory.repository_id,
                    )
                )
            cases.append(
                _CaseMaterial(
                    id=case_id,
                    partition=ReviewPartition.DIAGNOSTIC,
                    source_kind=ReviewSourceKind.REAL_WORKLOAD,
                    query=task.prompt,
                    query_repository_id=task.repository_id,
                    cutoff_time=task.cutoff,
                    candidates=tuple(candidates),
                )
            )
        if not cases:
            raise HumanReviewBuildError(f"real workload source produced no cases: {source_path}")
        snapshot = ReviewSourceSnapshot(
            id=manifest.name,
            kind=ReviewSourceKind.REAL_WORKLOAD,
            path=source_path,
            source_manifest_sha256=manifest.digest(),
            repositories=sorted(selected_repositories),
            cases=len(cases),
        )
        return cases, source_rows, snapshot

    def _assignment_cases(
        self,
        *,
        config: HumanReviewSourceConfig,
        assignment_id: str,
        assignment_index: int,
        cases: list[_CaseMaterial],
        primary_candidate_orders: dict[str, tuple[str, ...]],
    ) -> list[BlindReviewCase]:
        assigned: list[BlindReviewCase] = []
        for case in cases:
            ordered = sorted(
                case.candidates,
                key=lambda candidate: _ordering_digest(
                    config.sampling_seed,
                    assignment_id,
                    case.id,
                    candidate.id,
                ),
            )
            order = tuple(candidate.id for candidate in ordered)
            if assignment_index == 0:
                primary_candidate_orders[case.id] = order
            elif order == primary_candidate_orders[case.id] and len(ordered) > 1:
                ordered = [*ordered[1:], ordered[0]]
            assigned.append(
                BlindReviewCase(
                    assignment_id=assignment_id,
                    id=case.id,
                    partition=case.partition,
                    source_kind=case.source_kind,
                    query=case.query,
                    query_repository_id=case.query_repository_id,
                    cutoff_time=case.cutoff_time,
                    candidates=ordered,
                    rubric_version=config.rubric_version,
                )
            )
        return sorted(
            assigned,
            key=lambda case: _ordering_digest(
                config.sampling_seed, assignment_id, "case-order", case.id
            ),
        )


def _git_candidate(
    dataset_id: str, case_id: str, candidate: CalibrationCandidate
) -> BlindReviewCandidate:
    return BlindReviewCandidate(
        id=_opaque_id("candidate", dataset_id, case_id, candidate.id),
        repository_id=candidate.repository_id,
        observed_at=candidate.committed_at,
        title=candidate.title,
        content=candidate.text,
        source_paths=candidate.source_paths,
    )


def _time_stratified_sample(
    queries: list[CalibrationQuery], *, maximum: int, seed: str
) -> list[CalibrationQuery]:
    ordered = sorted(queries, key=lambda query: (query.cutoff_time, query.id))
    if len(ordered) <= maximum:
        return ordered
    selected: list[CalibrationQuery] = []
    for bucket in range(maximum):
        start = bucket * len(ordered) // maximum
        end = (bucket + 1) * len(ordered) // maximum
        choices = ordered[start:end]
        if not choices:
            raise HumanReviewBuildError("time-stratified sampling produced an empty bucket")
        selected.append(
            min(
                choices,
                key=lambda query: _ordering_digest(seed, str(bucket), query.id),
            )
        )
    return selected


def _resolve_repository_path(root: Path, relative: str, *, expect_directory: bool) -> Path:
    candidate = (root / Path(relative)).resolve(strict=True)
    if root not in candidate.parents:
        raise HumanReviewBuildError(f"source path escapes repository root: {relative}")
    if expect_directory != candidate.is_dir():
        expected = "directory" if expect_directory else "file"
        raise HumanReviewBuildError(f"source path must be a {expected}: {relative}")
    return candidate


def _reserve_source_id(source_id: str, observed: set[str]) -> None:
    if source_id in observed:
        raise HumanReviewBuildError(f"duplicate review source id: {source_id}")
    observed.add(source_id)


def _opaque_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _ordering_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _write_jsonl(
    root: Path,
    relative: str,
    rows: Sequence[BaseModel],
    *,
    exclude_none: bool = True,
) -> ReviewArtifactMetadata:
    encoded = "".join(
        json.dumps(
            row.model_dump(mode="json", exclude_none=exclude_none),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    return _write_bytes(root, relative, encoded, rows=len(rows))


def _write_json_bytes(root: Path, relative: str, encoded: bytes) -> ReviewArtifactMetadata:
    return _write_bytes(root, relative, encoded, rows=1)


def _write_bytes(root: Path, relative: str, encoded: bytes, *, rows: int) -> ReviewArtifactMetadata:
    path = (root / Path(relative)).resolve()
    if root != path and root not in path.parents:
        raise HumanReviewBuildError(f"output artifact escapes review root: {relative}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return ReviewArtifactMetadata(
        path=relative.replace("\\", "/"),
        sha256=hashlib.sha256(encoded).hexdigest(),
        rows=rows,
    )


def _write_manifest(path: Path, manifest: HumanReviewPackManifest) -> None:
    payload = manifest.model_dump(mode="json", exclude_none=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.write_bytes(encoded)


def _normalized_text_bytes(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HumanReviewBuildError(f"rubric is not UTF-8: {path}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8")


def generator_source_digest() -> str:
    source_paths = [
        Path(__file__),
        Path(__file__).with_name("human_review_models.py"),
        Path(__file__).with_name("human_review_audit.py"),
    ]
    digest = hashlib.sha256()
    for path in sorted(source_paths, key=lambda item: item.name):
        normalized = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized)
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "HumanReviewBuildError",
    "HumanReviewPackBuilder",
    "generator_source_digest",
]
