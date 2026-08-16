from __future__ import annotations

import argparse
import json
from pathlib import Path

from memoryos.evaluation.openai_compatible_coding_agent import tokenizer_artifact_sha256


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hash an already-downloaded tokenizer for exact local usage fallback."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    path = arguments.path.resolve(strict=True)
    print(
        json.dumps(
            {
                "kind": "huggingface",
                "model_path": str(path),
                "revision": arguments.revision,
                "tokenizer_sha256": tokenizer_artifact_sha256(path),
                "local_files_only": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
