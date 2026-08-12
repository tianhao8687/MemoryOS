from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.human_review_builder import HumanReviewPackBuilder
from memoryos.evaluation.human_review_models import load_human_review_source_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the blinded, dual-review MemoryOS human annotation pack."
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("benchmarks/human_review_v1/sources.json"),
    )
    parser.add_argument(
        "--rubric",
        type=Path,
        default=Path("benchmarks/human_review_v1/RUBRIC.md"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/human_review_v1/data"),
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    arguments = parser.parse_args()

    config = load_human_review_source_config(arguments.sources)
    manifest = HumanReviewPackBuilder().build(
        config,
        repository_root=arguments.repository_root,
        rubric_path=arguments.rubric,
        output_root=arguments.output,
    )
    print(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "digest": manifest.digest(),
                "status": manifest.status,
                "label_tier": manifest.label_tier,
                "cases": manifest.summary.cases,
                "judgments_per_reviewer": manifest.summary.judgments_per_reviewer,
                "test_split_sealed": manifest.test_split_sealed,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
