from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.public_bootstrap_training import train_public_bootstrap_profile


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train a non-production relative FTS/vector prior from the pinned public Git "
            "calibration dataset."
        )
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/calibration_v1/data"),
    )
    parser.add_argument("--output", type=Path, required=True)
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

    profile = train_public_bootstrap_profile(
        arguments.dataset,
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
                "status": profile.status,
                "profile_sha256": profile.profile_sha256,
                "source_dataset_sha256": profile.source_dataset_sha256,
                "relative_weights": profile.relative_weights,
                "candidate_beats_equal_weight_baseline_on_dev": (
                    profile.candidate_beats_equal_weight_baseline_on_dev
                ),
                "production_eligible": profile.production_eligible,
                "output": str(arguments.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
