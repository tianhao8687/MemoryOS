from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.public_shadow_replay import analyze_public_shadow_replay


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a completed public RRF shadow replay without rerunning retrieval."
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-rounds", type=int, default=4_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20_260_813)
    arguments = parser.parse_args()
    result = analyze_public_shadow_replay(
        arguments.report,
        output_path=arguments.output.resolve(),
        bootstrap_rounds=arguments.bootstrap_rounds,
        bootstrap_seed=arguments.bootstrap_seed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
