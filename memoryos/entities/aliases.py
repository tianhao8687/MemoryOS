from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

KNOWN_ALIASES = {
    "postgres": "postgresql",
    "postgresql db": "postgresql",
    "prod db": "production database",
    "production_database": "production database",
    "sqlite3": "sqlite",
    "fast api": "fastapi",
    "ts": "typescript",
    "js": "javascript",
    "py": "python",
    "node": "node.js",
    "nodejs": "node.js",
    "pnpmjs": "pnpm",
}


def normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower().replace("\\", "/")
    if "/" in normalized and not normalized.startswith(("http://", "https://", "ssh://")):
        normalized = str(PurePosixPath(normalized))
    normalized = re.sub(r"[^\w./:+#-]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    return KNOWN_ALIASES.get(normalized, normalized)
