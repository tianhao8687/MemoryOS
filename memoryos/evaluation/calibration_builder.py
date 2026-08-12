from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from memoryos.evaluation.calibration_models import (
    ArtifactMetadata,
    CalibrationArtifacts,
    CalibrationCandidate,
    CalibrationDatasetManifest,
    CalibrationJudgment,
    CalibrationQuery,
    CalibrationRepositorySnapshot,
    CalibrationRepositorySource,
    CalibrationSourceConfig,
    CalibrationSplit,
    CalibrationSummary,
    CandidateEligibility,
    SilverJudgmentEvidence,
    SplitArtifacts,
    load_calibration_dataset,
)

_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]{2,}")
_GENERIC_PREFIXES = (
    "bump ",
    "chore(deps",
    "chore: release",
    "deps:",
    "merge ",
    "prepare release",
    "release ",
)
_GENERIC_SUBJECTS = {
    "fix lint",
    "fix typo",
    "format",
    "update changelog",
    "update dependencies",
}
_TOKEN_STOPWORDS = {
    "add",
    "and",
    "change",
    "fix",
    "for",
    "from",
    "into",
    "remove",
    "test",
    "tests",
    "the",
    "this",
    "update",
    "use",
    "with",
}


class CalibrationBuildError(RuntimeError):
    """Raised when public source history cannot satisfy the dataset contract."""


@dataclass(frozen=True)
class GitCommitRecord:
    sha: str
    first_parent: str | None
    committed_at: datetime
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRelation:
    record: GitCommitRecord
    distance: int
    relevance: int
    shared_paths: tuple[str, ...]
    shared_directories: tuple[str, ...]
    lexical_overlap: int


@dataclass(frozen=True)
class QueryDraft:
    repository: CalibrationRepositorySource
    target: GitCommitRecord
    cutoff_commit: str
    cutoff_time: datetime
    local_candidates: tuple[CandidateRelation, ...]
    future_guards: tuple[GitCommitRecord, ...]
    has_lexical_negative: bool


class GitCalibrationClient:
    def __init__(self, *, executable: Path | None = None, timeout_seconds: int = 300) -> None:
        resolved = executable or _find_git()
        self.executable = resolved.resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        self._offline = False

    def set_offline(self, value: bool) -> None:
        self._offline = value

    def materialize(
        self,
        source: CalibrationRepositorySource,
        cache_root: Path,
        *,
        history_limit: int,
        offline: bool,
    ) -> Path:
        root = cache_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / source.id).resolve()
        if root not in target.parents:
            raise CalibrationBuildError(f"repository cache target escapes root: {source.id}")
        if target.exists():
            if not (target / ".git").is_dir():
                raise CalibrationBuildError(f"cache target is not a git repository: {target}")
            origin = self.text(target, "remote", "get-url", "origin").strip()
            if _normalize_remote(origin) != _normalize_remote(source.clone_url):
                raise CalibrationBuildError(
                    f"cache repository origin mismatch for {source.id}: {origin}"
                )
        else:
            if offline:
                raise CalibrationBuildError(f"offline cache is missing repository {source.id}")
            self._run(
                root,
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                f"--depth={history_limit}",
                source.clone_url,
                str(target),
                timeout_seconds=max(self.timeout_seconds, 900),
                retries=2,
            )
        if not self.has_commit(target, source.snapshot_commit):
            if offline:
                raise CalibrationBuildError(
                    f"offline repository {source.id} lacks {source.snapshot_commit}"
                )
            self._run(
                target,
                "fetch",
                "--filter=blob:none",
                f"--depth={history_limit}",
                "origin",
                source.snapshot_commit,
                timeout_seconds=max(self.timeout_seconds, 900),
                retries=2,
            )
        if not self.has_commit(target, source.snapshot_commit):
            raise CalibrationBuildError(f"repository {source.id} does not contain pinned snapshot")
        return target

    def has_commit(self, repository: Path, commit: str) -> bool:
        result = self._run(
            repository,
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
            check=False,
        )
        return result.returncode == 0

    def text(self, repository: Path, *arguments: str) -> str:
        return self.read_bytes(repository, *arguments).decode("utf-8", errors="replace")

    def read_bytes(self, repository: Path, *arguments: str) -> bytes:
        return self._run(repository, *arguments, retries=2).stdout

    def history(
        self, repository: Path, snapshot_commit: str, *, limit: int
    ) -> tuple[GitCommitRecord, ...]:
        raw = self.read_bytes(
            repository,
            "log",
            "--first-parent",
            f"--max-count={limit}",
            "--no-renames",
            "--date=iso-strict",
            "--format=%x1e%H%x1f%P%x1f%cI%x1f%s%x1f",
            "--name-only",
            snapshot_commit,
        ).decode("utf-8", errors="replace")
        records: list[GitCommitRecord] = []
        for chunk in raw.split("\x1e"):
            if not chunk.strip():
                continue
            parts = chunk.split("\x1f", maxsplit=4)
            if len(parts) != 5:
                raise CalibrationBuildError("git log emitted an unexpected record format")
            sha = parts[0].strip().lower()
            parents = parts[1].strip().lower().split()
            committed_at = datetime.fromisoformat(parts[2].strip())
            subject = _clean_subject(parts[3])
            paths = tuple(
                sorted(
                    {
                        normalized
                        for line in parts[4].splitlines()
                        if (normalized := _normalize_git_path(line)) is not None
                    }
                )
            )
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise CalibrationBuildError(f"git log emitted an invalid commit: {sha}")
            records.append(
                GitCommitRecord(
                    sha=sha,
                    first_parent=parents[0] if parents else None,
                    committed_at=committed_at,
                    subject=subject,
                    paths=paths,
                )
            )
        if not records or records[0].sha != snapshot_commit:
            raise CalibrationBuildError("pinned snapshot is not the first mined commit")
        return tuple(records)

    def _run(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
        timeout_seconds: int | None = None,
        retries: int = 0,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        if self._offline:
            environment["GIT_NO_LAZY_FETCH"] = "1"
        for attempt in range(retries + 1):
            try:
                result = subprocess.run(  # noqa: S603 - fixed git executable, argv only
                    [str(self.executable), *arguments],
                    cwd=cwd,
                    check=False,
                    capture_output=True,
                    timeout=timeout_seconds or self.timeout_seconds,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt < retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise CalibrationBuildError(f"git command timed out: {arguments[0]}") from exc
            if not check or result.returncode == 0:
                return result
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            if attempt < retries and _is_transient_git_failure(detail):
                time.sleep(min(2**attempt, 4))
                continue
            raise CalibrationBuildError(f"git {arguments[0]} failed: {detail}")
        raise AssertionError("git retry loop exhausted without returning")


class GitSilverCalibrationBuilder:
    def __init__(self, client: GitCalibrationClient | None = None) -> None:
        self.client = client or GitCalibrationClient()

    def materialize_sources(
        self,
        config: CalibrationSourceConfig,
        cache_root: Path,
        *,
        offline: bool = False,
    ) -> dict[str, Path]:
        self.client.set_offline(offline)
        return {
            source.id: self.client.materialize(
                source,
                cache_root,
                history_limit=config.settings.history_limit,
                offline=offline,
            )
            for source in config.repositories
        }

    def build(
        self,
        config: CalibrationSourceConfig,
        repository_paths: dict[str, Path],
        output_root: Path,
    ) -> CalibrationDatasetManifest:
        expected_repositories = {source.id for source in config.repositories}
        if set(repository_paths) != expected_repositories:
            raise CalibrationBuildError("repository path mapping does not match source config")

        histories: dict[str, tuple[GitCommitRecord, ...]] = {}
        snapshots: list[CalibrationRepositorySnapshot] = []
        for source in config.repositories:
            repository = repository_paths[source.id].resolve(strict=True)
            if not self.client.has_commit(repository, source.snapshot_commit):
                raise CalibrationBuildError(
                    f"repository {source.id} lacks pinned snapshot {source.snapshot_commit}"
                )
            records = self.client.history(
                repository,
                source.snapshot_commit,
                limit=config.settings.history_limit,
            )
            histories[source.id] = records
            tree = self.client.text(
                repository, "rev-parse", f"{source.snapshot_commit}^{{tree}}"
            ).strip()
            license_bytes = self.client.read_bytes(
                repository, "show", f"{source.snapshot_commit}:{source.license_path}"
            )
            snapshots.append(
                CalibrationRepositorySnapshot(
                    id=source.id,
                    role=source.role,
                    split=source.split,
                    language=source.language,
                    clone_url=source.clone_url,
                    source_url=source.source_url,
                    snapshot_commit=source.snapshot_commit,
                    snapshot_tree=tree,
                    snapshot_committed_at=records[0].committed_at,
                    license_spdx=source.license_spdx,
                    license_path=source.license_path,
                    license_url=source.license_url,
                    license_sha256=hashlib.sha256(license_bytes).hexdigest(),
                    mined_commits=len(records),
                )
            )

        drafts: list[QueryDraft] = []
        for source in config.repositories:
            if source.role.value != "query_source":
                continue
            repository_drafts = _draft_queries(source, histories[source.id], config)
            if len(repository_drafts) < config.settings.min_queries_per_repository:
                raise CalibrationBuildError(
                    f"repository {source.id} produced only {len(repository_drafts)} usable "
                    f"queries; minimum is {config.settings.min_queries_per_repository}"
                )
            drafts.extend(
                _spread_select(
                    repository_drafts,
                    config.settings.max_queries_per_repository,
                )
            )

        guard_records: list[tuple[CalibrationRepositorySource, GitCommitRecord]] = []
        for source in config.repositories:
            if source.role.value != "guard_only":
                continue
            guard_records.extend(
                (source, record)
                for record in histories[source.id]
                if record.paths
                and _usable_subject(
                    record.subject,
                    config.settings.min_query_tokens,
                )
            )
        if len(guard_records) < config.settings.cross_scope_guards_per_query:
            raise CalibrationBuildError("guard repositories do not contain enough usable commits")

        candidates: dict[str, CalibrationCandidate] = {}
        queries: dict[CalibrationSplit, list[CalibrationQuery]] = {
            split: [] for split in CalibrationSplit
        }
        judgments: dict[CalibrationSplit, list[CalibrationJudgment]] = {
            split: [] for split in CalibrationSplit
        }
        for draft in drafts:
            assert draft.repository.split is not None
            split = draft.repository.split
            query, query_judgments = _materialize_query(
                draft,
                guard_records,
                config,
                candidates,
            )
            queries[split].append(query)
            judgments[split].extend(query_judgments)

        root = output_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        candidate_rows = sorted(candidates.values(), key=lambda item: item.id)
        candidate_artifact = _write_jsonl(root, "candidates.jsonl", candidate_rows)
        split_artifacts: dict[CalibrationSplit, SplitArtifacts] = {}
        for split in CalibrationSplit:
            split_queries = sorted(queries[split], key=lambda item: item.id)
            split_judgments = sorted(
                judgments[split], key=lambda item: (item.query_id, item.candidate_id)
            )
            split_artifacts[split] = SplitArtifacts(
                queries=_write_jsonl(
                    root,
                    f"{split.value}/queries.jsonl",
                    split_queries,
                ),
                qrels=_write_jsonl(
                    root,
                    f"{split.value}/qrels.jsonl",
                    split_judgments,
                ),
            )

        all_judgments = [item for split in CalibrationSplit for item in judgments[split]]
        all_queries = [item for split in CalibrationSplit for item in queries[split]]
        summary = CalibrationSummary(
            repositories=len(config.repositories),
            query_repositories=sum(
                source.role.value == "query_source" for source in config.repositories
            ),
            candidates=len(candidates),
            queries=len(all_queries),
            judgments=len(all_judgments),
            queries_by_split={split.value: len(queries[split]) for split in CalibrationSplit},
            queries_by_repository=dict(
                sorted(Counter(item.repository_id for item in all_queries).items())
            ),
            relevance_counts=dict(
                sorted(Counter(str(item.relevance) for item in all_judgments).items())
            ),
            eligibility_counts=dict(
                sorted(Counter(item.eligibility.value for item in all_judgments).items())
            ),
        )
        manifest = CalibrationDatasetManifest(
            dataset_id=config.dataset_id,
            generator_version=config.generator_version,
            generated_at=config.generated_at,
            generator_source_sha256=generator_source_digest(),
            source_config_sha256=config.digest(),
            settings=config.settings,
            repositories=sorted(snapshots, key=lambda item: item.id),
            artifacts=CalibrationArtifacts(
                candidates=candidate_artifact,
                train=split_artifacts[CalibrationSplit.TRAIN],
                dev=split_artifacts[CalibrationSplit.DEV],
                test=split_artifacts[CalibrationSplit.TEST],
            ),
            summary=summary,
            label_policy=[
                "relevance=3 and required=true only when candidate and target touch an exact path",
                (
                    "only the nearest exact-path ancestor is required; other exact-path "
                    "ancestors remain relevance=3"
                ),
                "relevance=2 when candidate and target share an immediate parent directory",
                "relevance=1 when candidate and target share a top-level path and file suffix",
                "relevance=0 when no structural relationship above is present",
                "eligible candidates are first-parent ancestors of cutoff_commit",
                "future_guard candidates are first-parent descendants hidden behind the cutoff",
                "cross_scope_guard candidates come only from guard_only public repositories",
            ],
            limitations=[
                "Silver relevance is a deterministic Git path-overlap proxy, not human judgment.",
                "Commit subjects proxy task queries and may not match issue or agent wording.",
                (
                    "This dataset calibrates retrieval only; it cannot validate truth-judge "
                    "or health weights."
                ),
                (
                    "Source code and diffs are not redistributed; candidates contain commit "
                    "subjects and paths."
                ),
                (
                    "The test split is repository-held-out but public, so it is confirmatory "
                    "rather than secret."
                ),
            ],
            build_checks={
                "artifact_hashes_recorded": True,
                "exact_path_positive_present_per_query": True,
                "future_solution_not_in_runtime_payload": True,
                "git_first_parent_cutoff_enforced": True,
                "guard_repository_has_no_queries": True,
                "licenses_pinned_and_hashed": True,
                "qrels_separate_from_runtime_queries": True,
                "repository_held_out_splits": True,
            },
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        loaded = load_calibration_dataset(root)
        if loaded.digest != manifest.digest():
            raise CalibrationBuildError("written dataset digest differs from in-memory manifest")
        return manifest


def _draft_queries(
    source: CalibrationRepositorySource,
    records: tuple[GitCommitRecord, ...],
    config: CalibrationSourceConfig,
) -> list[QueryDraft]:
    settings = config.settings
    local_budget = (
        settings.candidate_pool_size
        - settings.future_guards_per_query
        - settings.cross_scope_guards_per_query
    )
    drafts: list[QueryDraft] = []
    for index, target in enumerate(records):
        if index < settings.future_guards_per_query:
            continue
        if index + 1 >= len(records):
            continue
        if target.first_parent != records[index + 1].sha:
            continue
        if not target.paths or len(target.paths) > settings.max_target_paths:
            continue
        if not _usable_subject(target.subject, settings.min_query_tokens):
            continue
        past = records[index + 1 : index + 1 + settings.lookback_commits]
        selected = _select_local_candidates(target, past, config, local_budget)
        if selected is None:
            continue
        future = tuple(
            record
            for record in reversed(records[:index])
            if record.paths and record.sha != target.sha
        )[: settings.future_guards_per_query]
        if len(future) != settings.future_guards_per_query:
            continue
        drafts.append(
            QueryDraft(
                repository=source,
                target=target,
                cutoff_commit=records[index + 1].sha,
                cutoff_time=records[index + 1].committed_at,
                local_candidates=selected,
                future_guards=future,
                has_lexical_negative=any(
                    item.relevance == 0 and item.lexical_overlap > 0 for item in selected
                ),
            )
        )
    return drafts


def _select_local_candidates(
    target: GitCommitRecord,
    past: tuple[GitCommitRecord, ...],
    config: CalibrationSourceConfig,
    budget: int,
) -> tuple[CandidateRelation, ...] | None:
    settings = config.settings
    relations = [
        _relationship(target, record, distance=index)
        for index, record in enumerate(past, start=1)
        if record.paths and record.sha != target.sha
    ]
    positives = sorted(
        (item for item in relations if item.relevance > 0),
        key=lambda item: (-item.relevance, item.distance, item.record.sha),
    )
    if not any(item.relevance == 3 for item in positives):
        return None
    selected = positives[: min(settings.max_positive_candidates, budget)]
    selected_ids = {item.record.sha for item in selected}

    lexical = sorted(
        (
            item
            for item in relations
            if item.relevance == 0
            and item.lexical_overlap > 0
            and item.record.sha not in selected_ids
        ),
        key=lambda item: (-item.lexical_overlap, item.distance, item.record.sha),
    )
    for item in lexical[: settings.lexical_negative_candidates]:
        if len(selected) >= budget:
            break
        selected.append(item)
        selected_ids.add(item.record.sha)

    recent = sorted(
        (item for item in relations if item.relevance == 0 and item.record.sha not in selected_ids),
        key=lambda item: (item.distance, item.record.sha),
    )
    for item in recent[: settings.recent_negative_candidates]:
        if len(selected) >= budget:
            break
        selected.append(item)
        selected_ids.add(item.record.sha)

    remaining = sorted(
        (item for item in relations if item.record.sha not in selected_ids),
        key=lambda item: (
            item.relevance > 0,
            hashlib.sha256(f"{target.sha}:{item.record.sha}".encode("ascii")).hexdigest(),
        ),
    )
    for item in remaining:
        if len(selected) >= budget:
            break
        selected.append(item)
        selected_ids.add(item.record.sha)
    if len(selected) != budget:
        return None
    return tuple(selected)


def _materialize_query(
    draft: QueryDraft,
    guard_records: list[tuple[CalibrationRepositorySource, GitCommitRecord]],
    config: CalibrationSourceConfig,
    candidates: dict[str, CalibrationCandidate],
) -> tuple[CalibrationQuery, list[CalibrationJudgment]]:
    settings = config.settings
    opaque_query_id = hashlib.sha256(
        f"{config.generator_version}\x00{draft.repository.id}\x00{draft.target.sha}".encode("ascii")
    ).hexdigest()[:24]
    query_id = f"{draft.repository.id}--q--{opaque_query_id}"
    entries: list[tuple[CalibrationCandidate, CalibrationJudgment]] = []
    required_commit = min(
        (relation for relation in draft.local_candidates if relation.relevance == 3),
        key=lambda relation: (relation.distance, relation.record.sha),
    ).record.sha
    for relation in draft.local_candidates:
        candidate = _candidate(draft.repository, relation.record, settings.max_candidate_paths)
        entries.append(
            (
                candidate,
                CalibrationJudgment(
                    query_id=query_id,
                    candidate_id=candidate.id,
                    relevance=relation.relevance,
                    eligibility=CandidateEligibility.ELIGIBLE,
                    required=relation.record.sha == required_commit,
                    forbidden=False,
                    evidence=SilverJudgmentEvidence(
                        target_commit=draft.target.sha,
                        target_paths=list(draft.target.paths),
                        shared_paths=list(relation.shared_paths),
                        shared_directories=list(relation.shared_directories),
                    ),
                ),
            )
        )
    for record in draft.future_guards:
        candidate = _candidate(draft.repository, record, settings.max_candidate_paths)
        entries.append(
            (
                candidate,
                _guard_judgment(
                    query_id,
                    candidate.id,
                    draft.target,
                    CandidateEligibility.FUTURE_GUARD,
                ),
            )
        )

    guard_order = sorted(
        guard_records,
        key=lambda item: hashlib.sha256(
            f"{draft.target.sha}:{item[0].id}:{item[1].sha}".encode("ascii")
        ).hexdigest(),
    )
    chosen_guard_ids: set[str] = set()
    for source, record in guard_order:
        candidate = _candidate(source, record, settings.max_candidate_paths)
        if candidate.id in chosen_guard_ids:
            continue
        entries.append(
            (
                candidate,
                _guard_judgment(
                    query_id,
                    candidate.id,
                    draft.target,
                    CandidateEligibility.CROSS_SCOPE_GUARD,
                ),
            )
        )
        chosen_guard_ids.add(candidate.id)
        if len(chosen_guard_ids) >= settings.cross_scope_guards_per_query:
            break
    if len(chosen_guard_ids) != settings.cross_scope_guards_per_query:
        raise CalibrationBuildError(f"query {query_id} lacks cross-scope guards")

    if len(entries) != settings.candidate_pool_size:
        raise CalibrationBuildError(f"query {query_id} candidate pool has wrong size")
    for candidate, _ in entries:
        existing = candidates.setdefault(candidate.id, candidate)
        if existing != candidate:
            raise CalibrationBuildError(f"candidate identity collision: {candidate.id}")

    ordered = sorted(
        entries,
        key=lambda item: hashlib.sha256(
            f"{draft.target.sha}:{item[0].id}:pool".encode("ascii")
        ).hexdigest(),
    )
    tags = [
        "cross-scope-guard",
        "future-guard",
        "path-overlap-silver",
        "recent-hard-negative",
        "repository-held-out",
    ]
    if draft.has_lexical_negative:
        tags.append("lexical-hard-negative")
    query = CalibrationQuery(
        id=query_id,
        repository_id=draft.repository.id,
        split=draft.repository.split,
        cutoff_commit=draft.cutoff_commit,
        cutoff_time=draft.cutoff_time,
        query=draft.target.subject,
        candidate_ids=[candidate.id for candidate, _ in ordered],
        challenge_tags=sorted(tags),
    )
    by_candidate = {judgment.candidate_id: judgment for _, judgment in entries}
    return query, [by_candidate[candidate_id] for candidate_id in query.candidate_ids]


def _guard_judgment(
    query_id: str,
    candidate_id: str,
    target: GitCommitRecord,
    eligibility: CandidateEligibility,
) -> CalibrationJudgment:
    return CalibrationJudgment(
        query_id=query_id,
        candidate_id=candidate_id,
        relevance=0,
        eligibility=eligibility,
        required=False,
        forbidden=True,
        evidence=SilverJudgmentEvidence(
            target_commit=target.sha,
            target_paths=list(target.paths),
        ),
    )


def _candidate(
    source: CalibrationRepositorySource,
    record: GitCommitRecord,
    max_paths: int,
) -> CalibrationCandidate:
    source_paths = list(record.paths[:max_paths])
    path_text = ", ".join(source_paths)
    text = f"{record.subject}\nChanged paths: {path_text}"[:5000]
    return CalibrationCandidate(
        id=f"{source.id}--{record.sha}",
        repository_id=source.id,
        source_commit=record.sha,
        committed_at=record.committed_at,
        title=record.subject[:500],
        text=text,
        source_paths=source_paths,
        scope_key=source.id,
        source_url=f"{str(source.source_url).rstrip('/')}/commit/{record.sha}",
    )


def _relationship(
    target: GitCommitRecord,
    candidate: GitCommitRecord,
    *,
    distance: int,
) -> CandidateRelation:
    target_paths = set(target.paths)
    candidate_paths = set(candidate.paths)
    shared_paths = tuple(sorted(target_paths & candidate_paths))
    target_directories = _parent_directories(target_paths)
    candidate_directories = _parent_directories(candidate_paths)
    shared_directories = tuple(sorted(target_directories & candidate_directories))
    if shared_paths:
        relevance = 3
    elif shared_directories:
        relevance = 2
    elif _shares_top_level_and_suffix(target_paths, candidate_paths):
        relevance = 1
    else:
        relevance = 0
    lexical_overlap = len(
        _informative_tokens(target.subject) & _informative_tokens(candidate.subject)
    )
    return CandidateRelation(
        record=candidate,
        distance=distance,
        relevance=relevance,
        shared_paths=shared_paths,
        shared_directories=shared_directories,
        lexical_overlap=lexical_overlap,
    )


def _parent_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for value in paths:
        parent = PurePosixPath(value).parent
        if parent.parts and parent.as_posix() != ".":
            directories.add(parent.as_posix())
    return directories


def _shares_top_level_and_suffix(left: set[str], right: set[str]) -> bool:
    left_pairs = {
        (path.parts[0], path.suffix.lower())
        for value in left
        if len((path := PurePosixPath(value)).parts) > 1 and path.suffix
    }
    right_pairs = {
        (path.parts[0], path.suffix.lower())
        for value in right
        if len((path := PurePosixPath(value)).parts) > 1 and path.suffix
    }
    return bool(left_pairs & right_pairs)


def _usable_subject(subject: str, min_tokens: int) -> bool:
    lowered = subject.lower().strip()
    if not lowered or lowered in _GENERIC_SUBJECTS:
        return False
    if lowered.startswith(_GENERIC_PREFIXES):
        return False
    if "dependabot" in lowered or "renovate" in lowered:
        return False
    return len(_informative_tokens(lowered)) >= min_tokens


def _informative_tokens(value: str) -> set[str]:
    return {
        token for token in _TOKEN_PATTERN.findall(value.lower()) if token not in _TOKEN_STOPWORDS
    }


def _spread_select(items: list[QueryDraft], maximum: int) -> list[QueryDraft]:
    if len(items) <= maximum:
        return items
    if maximum == 1:
        return [items[len(items) // 2]]
    positions = [round(index * (len(items) - 1) / (maximum - 1)) for index in range(maximum)]
    return [items[position] for position in positions]


def _write_jsonl(
    root: Path,
    relative_path: str,
    rows: list[Any],
) -> ArtifactMetadata:
    path = root / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(
            row.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    path.write_bytes(payload)
    return ArtifactMetadata(
        path=PurePosixPath(relative_path).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        rows=len(rows),
    )


def generator_source_digest() -> str:
    source_paths = [
        Path(__file__),
        Path(__file__).with_name("calibration_models.py"),
    ]
    digest = hashlib.sha256()
    for path in sorted(source_paths, key=lambda item: item.name):
        normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        digest.update(path.name.encode("ascii"))
        digest.update(b"\x00")
        digest.update(normalized)
        digest.update(b"\x00")
    return digest.hexdigest()


def _clean_subject(value: str) -> str:
    cleaned = _CONTROL_PATTERN.sub(" ", value)
    return " ".join(cleaned.split())[:500]


def _normalize_git_path(value: str) -> str | None:
    stripped = value.strip().replace("\\", "/")
    if not stripped:
        return None
    path = PurePosixPath(stripped)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        return None
    return path.as_posix()


def _normalize_remote(value: str) -> str:
    normalized = value.strip().rstrip("/")
    return normalized[:-4] if normalized.lower().endswith(".git") else normalized


def _is_transient_git_failure(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "connection reset",
            "could not resolve host",
            "failed to connect",
            "failed to receive handshake",
            "http 429",
            "operation timed out",
            "remote end hung up unexpectedly",
            "the requested url returned error: 502",
            "the requested url returned error: 503",
        )
    )


def _find_git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise CalibrationBuildError("git executable is required")
    return Path(executable)


__all__ = [
    "CalibrationBuildError",
    "GitCalibrationClient",
    "GitCommitRecord",
    "GitSilverCalibrationBuilder",
    "generator_source_digest",
]
