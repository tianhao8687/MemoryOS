from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.calibration_builder import GitSilverCalibrationBuilder
from memoryos.evaluation.calibration_models import load_calibration_source_config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the pinned public Git silver retrieval calibration dataset."
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("benchmarks/calibration_v1/sources.json"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("build/calibration-v1/repositories"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks/calibration_v1/data"),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require all pinned repositories to exist in --cache-dir.",
    )
    arguments = parser.parse_args()

    config = load_calibration_source_config(arguments.sources)
    builder = GitSilverCalibrationBuilder()
    repositories = builder.materialize_sources(
        config,
        arguments.cache_dir,
        offline=arguments.offline,
    )
    manifest = builder.build(config, repositories, arguments.output_dir)
    print(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "digest": manifest.digest(),
                "output": str(arguments.output_dir.resolve()),
                "summary": manifest.summary.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
