from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Train a non-production FTS/real-embedding prior from chronological SWE-Gym history."
        )
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pyarrow-path", type=Path, required=True)
    parser.add_argument("--fastembed-path", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--embedding-cache", type=Path, required=True)
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=128)
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

    sys.path.insert(0, str(arguments.fastembed_path.resolve(strict=True)))
    sys.path.insert(0, str(arguments.pyarrow_path.resolve(strict=True)))

    from memoryos.evaluation.fastembed_public_training import (
        build_fastembed_feature_bundle,
    )
    from memoryos.evaluation.public_bootstrap_training import (
        train_public_relevance_profile,
    )
    from memoryos.evaluation.swegym_public_training import (
        load_swegym_public_relevance_dataset,
    )

    dataset = load_swegym_public_relevance_dataset(
        arguments.dataset,
        lookback_tasks=arguments.lookback_tasks,
        min_candidates=arguments.min_candidates,
    )
    features = build_fastembed_feature_bundle(
        dataset,
        model_cache_dir=arguments.model_cache,
        embedding_cache_path=arguments.embedding_cache,
        model_name=arguments.model,
        threads=arguments.threads,
        batch_size=arguments.batch_size,
    )
    profile = train_public_relevance_profile(
        dataset,
        feature_rows=features.feature_rows,
        vector_channel_id=features.provider_id,
        vector_channel_source_sha256=features.provider_source_sha256,
        vector_feature_adapter_sha256=features.feature_adapter_sha256,
        vector_channel_limitations=features.limitations,
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
                "embedding_cache_sha256": features.embedding_cache_sha256,
                "feature_rows_sha256": profile.feature_rows_sha256,
                "metrics": {
                    split: value.model_dump(mode="json") for split, value in profile.metrics.items()
                },
                "model": {
                    "dimensions": features.dimensions,
                    "files_sha256": features.model_files_sha256,
                    "feature_adapter_sha256": features.feature_adapter_sha256,
                    "provider_id": features.provider_id,
                    "provider_source_sha256": features.provider_source_sha256,
                    "revision": features.model_revision,
                },
                "output": str(arguments.output.resolve()),
                "production_eligible": profile.production_eligible,
                "profile_sha256": profile.profile_sha256,
                "relative_weights": profile.relative_weights,
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
