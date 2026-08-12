from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import train_public_relevance_profile
from memoryos.evaluation.swegym_public_training import (
    build_swegym_public_relevance_dataset,
)


def _rows() -> list[dict[str, object]]:
    started = datetime(2025, 1, 1, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for repository_index in range(11):
        repository = f"example/repo-{repository_index:02d}"
        paths = [
            "src/shared.py",
            "docs/guide.md",
            "src/shared.py",
            "tests/test_other.py",
            "src/shared.py",
        ]
        for task_index, path in enumerate(paths):
            rows.append(
                {
                    "instance_id": f"repo-{repository_index:02d}-task-{task_index:02d}",
                    "repo": repository,
                    "created_at": (started + timedelta(days=task_index)).isoformat(),
                    "problem_statement": (
                        "repair shared implementation" if path == "src/shared.py" else "update docs"
                    ),
                    "patch": f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n",
                }
            )
    return rows


def test_swegym_history_adapter_is_deterministic_and_repository_held_out() -> None:
    source_sha256 = "a" * 64
    first = build_swegym_public_relevance_dataset(
        _rows(),
        source_file_sha256=source_sha256,
        lookback_tasks=4,
        min_candidates=2,
    )
    second = build_swegym_public_relevance_dataset(
        list(reversed(_rows())),
        source_file_sha256=source_sha256,
        lookback_tasks=4,
        min_candidates=2,
    )

    assert first == second
    repositories = {
        split: {query.repository_id for query in first.queries[split]} for split in CalibrationSplit
    }
    assert len(repositories[CalibrationSplit.TRAIN]) == 7
    assert len(repositories[CalibrationSplit.DEV]) == 2
    assert len(repositories[CalibrationSplit.TEST]) == 2
    assert not (repositories[CalibrationSplit.TRAIN] & repositories[CalibrationSplit.DEV])
    assert not (repositories[CalibrationSplit.TRAIN] & repositories[CalibrationSplit.TEST])
    assert not (repositories[CalibrationSplit.DEV] & repositories[CalibrationSplit.TEST])
    candidate_repository = {candidate.id: candidate.repository_id for candidate in first.candidates}
    for split in CalibrationSplit:
        for query in first.queries[split]:
            assert all(
                candidate_repository[candidate_id] == query.repository_id
                for candidate_id in query.candidate_ids
            )

    profile = train_public_relevance_profile(first, iterations=100)
    assert profile.production_eligible is False
    assert profile.training_repositories == sorted(repositories[CalibrationSplit.TRAIN])
    assert profile.metrics["train"].preference_pairs > 0
