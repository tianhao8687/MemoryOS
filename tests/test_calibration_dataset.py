from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memoryos.evaluation.calibration_builder import (
    GitSilverCalibrationBuilder,
    generator_source_digest,
)
from memoryos.evaluation.calibration_models import (
    CalibrationSourceConfig,
    CalibrationSplit,
    CandidateEligibility,
    load_calibration_dataset,
    load_calibration_source_config,
    load_runtime_split,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DATASET_ROOT = REPOSITORY_ROOT / "benchmarks" / "calibration_v1" / "data"
PUBLIC_SOURCE_CONFIG = REPOSITORY_ROOT / "benchmarks" / "calibration_v1" / "sources.json"
PUBLIC_DATASET_DIGEST = "52e670691d4c723680f7d2c67efcce31701001c88218bd3d915c82de5013eb3a"


def _git(root: Path, *arguments: str, at: str | None = None) -> str:
    executable = shutil.which("git")
    assert executable is not None
    environment = os.environ.copy()
    if at:
        environment.update({"GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at})
    result = subprocess.run(  # noqa: S603 - test fixture invokes local git with argv
        [executable, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _commit(root: Path, filename: str, content: str, subject: str, at: str) -> str:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", filename)
    _git(root, "commit", "-m", subject, at=at)
    return _git(root, "rev-parse", "HEAD")


def _repository(root: Path, identity: str) -> tuple[Path, str]:
    repository = root / identity
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "calibration@example.invalid")
    _git(repository, "config", "user.name", "Calibration Fixture")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    _commit(
        repository,
        "LICENSE",
        "MIT License\nPermission is hereby granted.\n",
        "Establish project license metadata",
        start.isoformat(),
    )
    snapshot = ""
    for index in range(1, 34):
        snapshot = _commit(
            repository,
            f"src/core/component_{index % 4}.py",
            f"VALUE = {index}\n",
            f"Improve parser behavior for component iteration {index}",
            (start + timedelta(days=index)).isoformat(),
        )
    return repository, snapshot


@pytest.fixture(scope="module")
def source_history(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[CalibrationSourceConfig, dict[str, Path]]:
    tmp_path = tmp_path_factory.mktemp("calibration-history")
    repositories: dict[str, Path] = {}
    source_rows: list[dict[str, Any]] = []
    roles = [
        ("train-project", "query_source", "train"),
        ("dev-project", "query_source", "dev"),
        ("test-project", "query_source", "test"),
        ("guard-project", "guard_only", None),
    ]
    for identity, role, split in roles:
        repository, snapshot = _repository(tmp_path, identity)
        repositories[identity] = repository
        row: dict[str, Any] = {
            "id": identity,
            "role": role,
            "language": "python",
            "clone_url": f"https://example.invalid/{identity}.git",
            "source_url": f"https://example.invalid/{identity}",
            "snapshot_commit": snapshot,
            "license_spdx": "MIT",
            "license_path": "LICENSE",
            "license_url": f"https://example.invalid/{identity}/blob/{snapshot}/LICENSE",
        }
        if split:
            row["split"] = split
        source_rows.append(row)
    config = CalibrationSourceConfig.model_validate(
        {
            "dataset_id": "calibration-fixture",
            "generated_at": "2026-08-12T08:00:00Z",
            "settings": {
                "history_limit": 50,
                "lookback_commits": 20,
                "max_queries_per_repository": 2,
                "min_queries_per_repository": 2,
                "candidate_pool_size": 8,
                "max_positive_candidates": 3,
                "lexical_negative_candidates": 1,
                "recent_negative_candidates": 2,
                "future_guards_per_query": 1,
                "cross_scope_guards_per_query": 1,
                "min_query_tokens": 2,
                "max_target_paths": 3,
                "max_candidate_paths": 5,
            },
            "repositories": source_rows,
        }
    )
    return config, repositories


def test_git_silver_dataset_is_deterministic_and_gold_isolated(
    tmp_path: Path,
    source_history: tuple[CalibrationSourceConfig, dict[str, Path]],
) -> None:
    config, repositories = source_history
    builder = GitSilverCalibrationBuilder()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    first = builder.build(config, repositories, first_root)
    second = builder.build(config, repositories, second_root)

    assert first.digest() == second.digest()
    assert first.summary.queries == 6
    assert first.summary.queries_by_split == {"train": 2, "dev": 2, "test": 2}
    assert first.summary.judgments == 6 * 8
    assert (first_root / "candidates.jsonl").read_bytes() == (
        second_root / "candidates.jsonl"
    ).read_bytes()

    manifest, candidates, runtime_queries = load_runtime_split(first_root, CalibrationSplit.TEST)
    assert manifest.digest() == first.digest()
    assert candidates
    assert len(runtime_queries) == 2
    forbidden_runtime_keys = {
        "eligibility",
        "relevance",
        "target_commit",
        "target_paths",
    }
    for query in runtime_queries:
        assert forbidden_runtime_keys.isdisjoint(query.model_dump(mode="json"))

    bundle = load_calibration_dataset(first_root)
    test_judgments = bundle.judgments[CalibrationSplit.TEST]
    assert {item.eligibility for item in test_judgments} == {
        CandidateEligibility.ELIGIBLE,
        CandidateEligibility.FUTURE_GUARD,
        CandidateEligibility.CROSS_SCOPE_GUARD,
    }
    assert all(item.relevance == 3 for item in test_judgments if item.required)


def test_dataset_loader_rejects_artifact_tampering(
    tmp_path: Path,
    source_history: tuple[CalibrationSourceConfig, dict[str, Path]],
) -> None:
    config, repositories = source_history
    output = tmp_path / "dataset"
    GitSilverCalibrationBuilder().build(config, repositories, output)
    with (output / "candidates.jsonl").open("ab") as handle:
        handle.write(b"{}\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_calibration_dataset(output)


def test_checked_in_public_silver_dataset_is_pinned_and_valid() -> None:
    bundle = load_calibration_dataset(PUBLIC_DATASET_ROOT)
    source_config = load_calibration_source_config(PUBLIC_SOURCE_CONFIG)

    assert bundle.digest == PUBLIC_DATASET_DIGEST
    assert bundle.manifest.source_config_sha256 == source_config.digest()
    assert bundle.manifest.generator_source_sha256 == generator_source_digest()
    assert bundle.manifest.summary.queries_by_split == {
        "train": 200,
        "dev": 50,
        "test": 50,
    }
    assert bundle.manifest.summary.candidates == 3656
    assert bundle.manifest.summary.judgments == 9600
    assert all(
        sum(judgment.required for judgment in bundle.judgments[split]) == len(bundle.queries[split])
        for split in CalibrationSplit
    )
