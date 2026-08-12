from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.model_review_report import validate_model_review_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the checked-in provisional model-only review bundle."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/human_review_v1/data"),
    )
    parser.add_argument(
        "--calibration-dataset",
        type=Path,
        default=Path("benchmarks/calibration_v1/data"),
    )
    parser.add_argument(
        "--model-review",
        type=Path,
        default=Path("benchmarks/human_review_v1/model_review"),
    )
    arguments = parser.parse_args()
    result = validate_model_review_bundle(
        dataset_root=arguments.dataset,
        calibration_root=arguments.calibration_dataset,
        model_review_root=arguments.model_review,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
