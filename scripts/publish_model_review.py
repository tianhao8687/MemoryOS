from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.model_review_report import publish_model_review_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and publish the provisional model-only blind-review bundle."
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
        "--review-a",
        type=Path,
        default=Path("build/human-review/model-reviewer-a2.responses.jsonl"),
    )
    parser.add_argument(
        "--review-b",
        type=Path,
        default=Path("build/human-review/model-reviewer-b.responses.jsonl"),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("build/human-review/ADJUDICATION_POLICY.md"),
    )
    parser.add_argument(
        "--packet",
        type=Path,
        default=Path("build/human-review/model-adjudication-packet.jsonl"),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("build/human-review/third-party-decision-plan.json"),
    )
    parser.add_argument(
        "--resolutions",
        type=Path,
        default=Path("build/human-review/model-disagreement-resolutions.jsonl"),
    )
    parser.add_argument(
        "--adjudication",
        type=Path,
        default=Path("build/human-review/model-adjudicated-provisional.jsonl"),
    )
    parser.add_argument(
        "--protocol-audit",
        type=Path,
        default=Path("benchmarks/human_review_v1/model_review_protocol_audit.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/human_review_v1/model_review"),
    )
    arguments = parser.parse_args()
    report = publish_model_review_bundle(
        dataset_root=arguments.dataset,
        calibration_root=arguments.calibration_dataset,
        review_a_path=arguments.review_a,
        review_b_path=arguments.review_b,
        policy_path=arguments.policy,
        packet_path=arguments.packet,
        plan_path=arguments.plan,
        resolutions_path=arguments.resolutions,
        adjudication_path=arguments.adjudication,
        protocol_audit_path=arguments.protocol_audit,
        output_root=arguments.output,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
