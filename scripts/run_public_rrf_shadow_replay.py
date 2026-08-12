from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a public FTS/vector RRF shadow through the real MemoryOS pipeline."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--pyarrow-path", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--embedding-base-url", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--split", choices=["dev", "test"], default="test")
    parser.add_argument("--queries-per-repository", type=int, default=5)
    parser.add_argument("--sample-seed", default="memoryos-public-rrf-replay-v1")
    arguments = parser.parse_args()
    sys.path.insert(0, str(arguments.pyarrow_path.resolve(strict=True)))

    from memoryos.evaluation.calibration_models import CalibrationSplit
    from memoryos.evaluation.public_shadow_replay import run_public_shadow_replay
    from memoryos.evaluation.swegym_public_training import (
        load_swegym_public_relevance_dataset,
    )
    from memoryos.retrieval_v2.rrf_shadow import load_rrf_channel_shadow_profile

    dataset = load_swegym_public_relevance_dataset(arguments.dataset)
    profile = load_rrf_channel_shadow_profile(arguments.profile)
    result = run_public_shadow_replay(
        dataset,
        profile,
        output_path=arguments.output.resolve(),
        state_root=arguments.state_root.resolve(),
        embedding_base_url=arguments.embedding_base_url,
        embedding_model=arguments.embedding_model,
        split=CalibrationSplit(arguments.split),
        queries_per_repository=arguments.queries_per_repository,
        sample_seed=arguments.sample_seed,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "production_eligible": result["production_eligible"],
                "query_count": result["query_count"],
                "repository_count": result["repository_count"],
                "metrics": result["metrics"],
                "output": str(arguments.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
