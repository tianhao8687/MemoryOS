from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.human_review_models import (
    load_adjudication,
    load_completed_review,
    load_human_review_pack,
    review_agreement,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a blind review pack and optional completed reviews/adjudication."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/human_review_v1/data"),
    )
    parser.add_argument(
        "--response",
        action="append",
        default=[],
        type=Path,
        help="Completed reviewer response JSONL; repeat for independent reviewers.",
    )
    parser.add_argument(
        "--adjudication",
        type=Path,
        help="Final adjudication JSONL; requires at least two --response files.",
    )
    arguments = parser.parse_args()

    bundle = load_human_review_pack(arguments.dataset)
    completed = [load_completed_review(bundle, path) for path in arguments.response]
    result: dict[str, object] = {
        "dataset_id": bundle.manifest.dataset_id,
        "digest": bundle.digest,
        "status": bundle.manifest.status,
        "cases": bundle.manifest.summary.cases,
        "judgments_per_reviewer": bundle.manifest.summary.judgments_per_reviewer,
        "completed_reviews": len(completed),
        "test_split_sealed": bundle.manifest.test_split_sealed,
        "coupling_status": bundle.coupling_audit.get("status"),
    }
    if len(completed) >= 2:
        result["agreement"] = review_agreement(completed[0], completed[1])
    if arguments.adjudication is not None:
        adjudicated = load_adjudication(bundle, arguments.response, arguments.adjudication)
        result["adjudicated_rows"] = len(adjudicated)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
