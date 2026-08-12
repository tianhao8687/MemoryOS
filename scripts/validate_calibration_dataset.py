from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.calibration_models import load_calibration_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify calibration artifact hashes, schemas, splits, and references."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/calibration_v1/data"),
    )
    arguments = parser.parse_args()
    bundle = load_calibration_dataset(arguments.dataset)
    print(
        json.dumps(
            {
                "dataset_id": bundle.manifest.dataset_id,
                "digest": bundle.digest,
                "status": "valid",
                "summary": bundle.manifest.summary.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
