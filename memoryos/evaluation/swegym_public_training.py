from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import (
    PublicRelevanceCandidate,
    PublicRelevanceDataset,
    PublicRelevanceJudgment,
    PublicRelevanceQuery,
)

SWEGYM_SPLIT_SEED = "swegym-public-bootstrap-v1"

_DIFF_PATH_PATTERN = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)


@dataclass(frozen=True)
class _SWEGymTask:
    instance_id: str
    repository_id: str
    created_at: datetime
    problem_statement: str
    changed_paths: tuple[str, ...]


def load_swegym_public_relevance_dataset(
    parquet_path: Path,
    *,
    lookback_tasks: int = 100,
    min_candidates: int = 16,
    split_seed: str = SWEGYM_SPLIT_SEED,
) -> PublicRelevanceDataset:
    resolved = parquet_path.resolve(strict=True)
    try:
        parquet = importlib.import_module("pyarrow.parquet")
    except ImportError as exc:  # pragma: no cover - CI intentionally has no pyarrow
        raise RuntimeError(
            "reading SWE-Gym parquet requires optional pyarrow on PYTHONPATH"
        ) from exc
    table: Any = parquet.read_table(
        resolved,
        columns=["instance_id", "repo", "created_at", "problem_statement", "patch"],
    )
    raw_rows: Any = table.to_pylist()
    if not isinstance(raw_rows, list):
        raise ValueError("SWE-Gym parquet did not produce a row list")
    rows = [cast(Mapping[str, object], row) for row in raw_rows if isinstance(row, Mapping)]
    if len(rows) != len(raw_rows):
        raise ValueError("SWE-Gym parquet contains a non-object row")
    return build_swegym_public_relevance_dataset(
        rows,
        source_file_sha256=_file_sha256(resolved),
        lookback_tasks=lookback_tasks,
        min_candidates=min_candidates,
        split_seed=split_seed,
    )


def build_swegym_public_relevance_dataset(
    rows: Sequence[Mapping[str, object]],
    *,
    source_file_sha256: str,
    lookback_tasks: int = 100,
    min_candidates: int = 16,
    split_seed: str = SWEGYM_SPLIT_SEED,
) -> PublicRelevanceDataset:
    if not _is_sha256(source_file_sha256):
        raise ValueError("source_file_sha256 must be a lowercase SHA-256")
    if lookback_tasks < 2 or min_candidates < 2 or min_candidates > lookback_tasks:
        raise ValueError("SWE-Gym lookback must cover at least two candidates")
    if not split_seed.strip():
        raise ValueError("SWE-Gym split seed cannot be empty")

    tasks = tuple(_parse_task(row) for row in rows)
    if len({task.instance_id for task in tasks}) != len(tasks):
        raise ValueError("SWE-Gym instance IDs must be unique")
    by_repository: dict[str, list[_SWEGymTask]] = defaultdict(list)
    for task in tasks:
        if task.changed_paths:
            by_repository[task.repository_id].append(task)
    if len(by_repository) < 3:
        raise ValueError("SWE-Gym public bootstrap requires at least three repositories")
    split_by_repository = _repository_splits(tuple(by_repository), split_seed)

    candidates = tuple(
        PublicRelevanceCandidate(
            id=task.instance_id,
            repository_id=task.repository_id,
            text=(
                task.problem_statement
                + "\nHistorical changed paths: "
                + ", ".join(task.changed_paths)
            ),
        )
        for task in sorted(tasks, key=lambda item: item.instance_id)
        if task.changed_paths
    )
    queries: dict[CalibrationSplit, list[PublicRelevanceQuery]] = {
        split: [] for split in CalibrationSplit
    }
    judgments: dict[CalibrationSplit, list[PublicRelevanceJudgment]] = {
        split: [] for split in CalibrationSplit
    }
    for repository_id, repository_tasks in sorted(by_repository.items()):
        ordered = sorted(
            repository_tasks,
            key=lambda item: (item.created_at, item.instance_id),
        )
        split = split_by_repository[repository_id]
        for target_index, target in enumerate(ordered):
            prior = [
                candidate
                for candidate in ordered[max(0, target_index - lookback_tasks) : target_index]
                if candidate.created_at < target.created_at
            ]
            if len(prior) < min_candidates:
                continue
            relevance_by_candidate = {
                candidate.instance_id: _path_relevance(
                    target.changed_paths,
                    candidate.changed_paths,
                )
                for candidate in prior
            }
            exact_candidates = [
                candidate
                for candidate in prior
                if relevance_by_candidate[candidate.instance_id] == 3
            ]
            if not exact_candidates:
                continue
            required_id = exact_candidates[-1].instance_id
            candidate_ids = tuple(
                candidate.instance_id
                for candidate in sorted(
                    prior,
                    key=lambda item: hashlib.sha256(
                        f"{target.instance_id}:{item.instance_id}:pool".encode()
                    ).hexdigest(),
                )
            )
            queries[split].append(
                PublicRelevanceQuery(
                    query_id=target.instance_id,
                    repository_id=repository_id,
                    split=split,
                    query=target.problem_statement,
                    candidate_ids=candidate_ids,
                )
            )
            judgments[split].extend(
                PublicRelevanceJudgment(
                    query_id=target.instance_id,
                    candidate_id=candidate_id,
                    relevance=relevance_by_candidate[candidate_id],
                    eligible=True,
                    required=candidate_id == required_id,
                )
                for candidate_id in candidate_ids
            )

    if any(not queries[split] for split in CalibrationSplit):
        raise ValueError("SWE-Gym split policy produced an empty query partition")
    adapter_sha256 = swegym_public_adapter_digest()
    dataset_identity = {
        "adapter_sha256": adapter_sha256,
        "lookback_tasks": lookback_tasks,
        "min_candidates": min_candidates,
        "repository_splits": {
            repository: split.value for repository, split in sorted(split_by_repository.items())
        },
        "source_file_sha256": source_file_sha256,
        "split_seed": split_seed,
    }
    return PublicRelevanceDataset(
        dataset_id="swe-gym-history-silver-v1",
        dataset_sha256=_canonical_hash(dataset_identity),
        source_adapter_sha256=adapter_sha256,
        candidates=candidates,
        queries={split: tuple(queries[split]) for split in CalibrationSplit},
        judgments={split: tuple(judgments[split]) for split in CalibrationSplit},
        limitations=(
            "SWE-Gym gold patch paths are scorer-only labels; they are never included in target "
            "query features.",
            "Labels measure historical changed-path overlap, not downstream agent success.",
            "Candidate text contains only earlier issue text and earlier changed paths from the "
            "same repository.",
            "The raw SWE-Gym created_at field does not prove when an earlier gold patch became "
            "available; this adapter remains diagnostic until merge-time provenance is bound.",
            "Repository train/dev/test assignment is a hash split fixed independently of labels.",
            "The dataset contains no cross-scope, future-information, graph, truth, or feedback "
            "calibration labels.",
        ),
    )


def swegym_public_adapter_digest() -> str:
    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_task(row: Mapping[str, object]) -> _SWEGymTask:
    instance_id = _required_text(row.get("instance_id"), "instance_id")
    repository_id = _required_text(row.get("repo"), "repo")
    problem_statement = _required_text(row.get("problem_statement"), "problem_statement")
    patch = _required_text(row.get("patch"), "patch")
    created_at = _parse_datetime(row.get("created_at"))
    paths = tuple(
        sorted(
            {
                normalized
                for match in _DIFF_PATH_PATTERN.finditer(patch)
                if (normalized := _normalize_path(match.group(2))) is not None
            }
        )
    )
    return _SWEGymTask(
        instance_id=instance_id,
        repository_id=repository_id,
        created_at=created_at,
        problem_statement=problem_statement,
        changed_paths=paths,
    )


def _repository_splits(
    repositories: tuple[str, ...],
    split_seed: str,
) -> dict[str, CalibrationSplit]:
    ordered = sorted(
        repositories,
        key=lambda repository: hashlib.sha256(f"{split_seed}:{repository}".encode()).hexdigest(),
    )
    partition_size = max(1, len(ordered) // 5)
    if len(ordered) - 2 * partition_size < 1:
        raise ValueError("SWE-Gym repository split leaves no training repository")
    train_end = len(ordered) - 2 * partition_size
    dev_end = len(ordered) - partition_size
    return {
        repository: (
            CalibrationSplit.TRAIN
            if index < train_end
            else CalibrationSplit.DEV
            if index < dev_end
            else CalibrationSplit.TEST
        )
        for index, repository in enumerate(ordered)
    }


def _path_relevance(target_paths: tuple[str, ...], candidate_paths: tuple[str, ...]) -> int:
    target = set(target_paths)
    candidate = set(candidate_paths)
    if target & candidate:
        return 3
    if _parent_directories(target) & _parent_directories(candidate):
        return 2
    if _top_level_suffixes(target) & _top_level_suffixes(candidate):
        return 1
    return 0


def _parent_directories(paths: set[str]) -> set[str]:
    return {
        parent.as_posix()
        for value in paths
        if (parent := PurePosixPath(value).parent).parts and parent.as_posix() != "."
    }


def _top_level_suffixes(paths: set[str]) -> set[tuple[str, str]]:
    return {
        (path.parts[0], path.suffix.lower())
        for value in paths
        if len((path := PurePosixPath(value)).parts) > 1 and path.suffix
    }


def _normalize_path(value: str) -> str | None:
    stripped = value.strip().replace("\\", "/")
    if not stripped or stripped == "/dev/null":
        return None
    path = PurePosixPath(stripped)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        return None
    return path.as_posix()


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("SWE-Gym created_at must be a non-empty string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SWE-Gym {field_name} must be a non-empty string")
    return value.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "SWEGYM_SPLIT_SEED",
    "build_swegym_public_relevance_dataset",
    "load_swegym_public_relevance_dataset",
    "swegym_public_adapter_digest",
]
