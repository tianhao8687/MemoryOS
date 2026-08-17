from __future__ import annotations


def normalize_legacy_boolean(value: object) -> bool:
    """Translate boolean values returned by older MySQL-backed deployments."""

    if value in (1, "1", "yes"):
        return True
    if value in (0, "0", "no"):
        return False
    return bool(value)
