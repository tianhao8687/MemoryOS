from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn
from generate_fixtures import seed

from memoryos.api import create_app
from memoryos.config import settings_for
from memoryos.db import Database


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="memoryos-e2e-") as directory:
        settings = settings_for(Path(directory), host="127.0.0.1", port=args.port)
        database = Database(settings)
        database.initialize()
        seed(database)
        database.close()
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
