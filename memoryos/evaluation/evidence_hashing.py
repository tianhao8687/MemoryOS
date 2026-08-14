from __future__ import annotations

import hashlib
from pathlib import Path

_TEXT_EVIDENCE_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".patch"})


def canonical_file_sha256(path: Path) -> str:
    """Hash text evidence independently of checkout line-ending policy."""
    payload = path.read_bytes()
    if path.suffix.lower() in _TEXT_EVIDENCE_SUFFIXES:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["canonical_file_sha256"]
