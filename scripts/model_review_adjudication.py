from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.model_review_adjudication import (
    draft_model_resolutions,
    draft_model_resolutions_from_plan,
    finalize_model_adjudication,
    prepare_model_adjudication,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or finalize third-party adjudication of two blind model reviews."
    )
    parser.add_argument("mode", choices=("prepare", "draft", "finalize"))
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/human_review_v1/data"),
    )
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument(
        "--packet",
        type=Path,
        default=Path("build/human-review/model-adjudication-packet.jsonl"),
    )
    parser.add_argument("--resolutions", type=Path)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/human-review/model-adjudicated.jsonl"),
    )
    arguments = parser.parse_args()

    if arguments.mode == "prepare":
        report = prepare_model_adjudication(
            dataset_root=arguments.dataset,
            review_a_path=arguments.review_a,
            review_b_path=arguments.review_b,
            packet_path=arguments.packet,
        )
    elif arguments.mode == "draft":
        if (arguments.overrides is None) == (arguments.plan is None):
            parser.error("draft requires exactly one of --overrides or --plan")
        if arguments.plan is not None:
            report = draft_model_resolutions_from_plan(
                packet_path=arguments.packet,
                plan_path=arguments.plan,
                output_path=arguments.output,
            )
        else:
            assert arguments.overrides is not None
            report = draft_model_resolutions(
                packet_path=arguments.packet,
                overrides_path=arguments.overrides,
                output_path=arguments.output,
            )
    else:
        if arguments.resolutions is None:
            parser.error("finalize requires --resolutions")
        report = finalize_model_adjudication(
            dataset_root=arguments.dataset,
            review_a_path=arguments.review_a,
            review_b_path=arguments.review_b,
            resolutions_path=arguments.resolutions,
            output_path=arguments.output,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
