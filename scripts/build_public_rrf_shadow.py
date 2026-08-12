from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.public_shadow import (
    load_public_bootstrap_profile,
    rrf_channel_shadow_from_public,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a non-production public FTS/vector prior into a frozen RRF shadow."
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if output.exists():
        raise ValueError(f"refusing to overwrite RRF shadow profile: {output}")
    source = load_public_bootstrap_profile(arguments.profile)
    shadow = rrf_channel_shadow_from_public(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(shadow.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "production_eligible": shadow.production_eligible,
                "production_weights_changed": shadow.production_weights_changed,
                "shadow_profile_sha256": shadow.digest(),
                "source_public_profile_sha256": shadow.source_public_profile_sha256,
                "channel_weights": shadow.channel_weights,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
