from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.retrieval_v2.routing import (
    APPROVED_RETRIEVAL_RECIPES,
    RetrievalRoutingShadowProfile,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic, non-production profile for the approved retrieval recipe "
            "router."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if output.exists():
        raise ValueError(f"refusing to overwrite retrieval routing shadow profile: {output}")

    profile = RetrievalRoutingShadowProfile()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "production_eligible": profile.production_eligible,
                "production_behavior_changed": profile.production_behavior_changed,
                "routing_profile_sha256": profile.digest(),
                "recipe_registry_sha256": profile.recipe_registry_sha256,
                "recipes": {
                    recipe_id: recipe.model_dump(mode="json")
                    for recipe_id, recipe in sorted(APPROVED_RETRIEVAL_RECIPES.items())
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
