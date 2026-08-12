from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from memoryos.evaluation.public_bootstrap_training import train_public_relevance_profile
from memoryos.evaluation.swegym_public_training import load_swegym_public_relevance_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train a non-production relative FTS/vector prior from chronological SWE-Gym history."
        )
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pyarrow-path", type=Path)
    parser.add_argument("--lookback-tasks", type=int, default=100)
    parser.add_argument("--min-candidates", type=int, default=16)
    parser.add_argument(
        "--l2-candidates",
        type=float,
        nargs="+",
        default=[0.001, 0.005, 0.02, 0.08, 0.32, 1.28],
    )
    parser.add_argument("--iterations", type=int, default=2500)
    parser.add_argument("--learning-rate", type=float, default=0.25)
    parser.add_argument("--max-pairs-per-query", type=int, default=256)
    arguments = parser.parse_args()

    if arguments.pyarrow_path is not None:
        sys.path.insert(0, str(arguments.pyarrow_path.resolve(strict=True)))
    dataset = load_swegym_public_relevance_dataset(
        arguments.dataset,
        lookback_tasks=arguments.lookback_tasks,
        min_candidates=arguments.min_candidates,
    )
    profile = train_public_relevance_profile(
        dataset,
        l2_candidates=arguments.l2_candidates,
        iterations=arguments.iterations,
        learning_rate=arguments.learning_rate,
        max_preference_pairs_per_query=arguments.max_pairs_per_query,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "candidate_beats_equal_weight_baseline_on_dev": (
                    profile.candidate_beats_equal_weight_baseline_on_dev
                ),
                "metrics": {
                    split: value.model_dump(mode="json") for split, value in profile.metrics.items()
                },
                "output": str(arguments.output.resolve()),
                "production_eligible": profile.production_eligible,
                "profile_sha256": profile.profile_sha256,
                "relative_weights": profile.relative_weights,
                "repositories": {
                    "train": profile.training_repositories,
                    "dev": profile.development_repositories,
                    "test": profile.test_repositories,
                },
                "source_dataset_sha256": profile.source_dataset_sha256,
                "status": profile.status,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
