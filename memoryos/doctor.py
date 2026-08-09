from __future__ import annotations

import os
from typing import Any

from sqlalchemy import text

from memoryos.config import MemoryOSSettings
from memoryos.db.session import Database
from memoryos.security.token import TokenManager


def run_doctor(database: Database, settings: MemoryOSSettings) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        integrity = database.integrity_check()
        add("database_integrity", "PASS" if integrity == "ok" else "FAIL", integrity)
    except Exception as exc:  # pragma: no cover - diagnostic boundary
        add("database_integrity", "FAIL", str(exc))
    try:
        with database.engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM memory_fts")).scalar_one()
        add("fts5", "PASS", "memory_fts is queryable")
    except Exception as exc:  # pragma: no cover - diagnostic boundary
        add("fts5", "FAIL", str(exc))
    token = TokenManager(settings.token_path).get_or_create()
    add(
        "local_token", "PASS" if len(token) >= 40 else "FAIL", "present with restricted permissions"
    )
    add(
        "loopback_binding",
        "PASS" if settings.host in {"127.0.0.1", "localhost", "::1"} else "FAIL",
        settings.host,
    )
    add(
        "data_directory",
        "PASS" if os.access(settings.data_dir, os.W_OK) else "FAIL",
        str(settings.data_dir),
    )
    add(
        "management_ui",
        "PASS" if (settings.web_dist / "index.html").exists() else "WARN",
        str(settings.web_dist),
    )
    add(
        "embedding_provider",
        "PASS" if settings.embedding_base_url and settings.embedding_model else "WARN",
        "configured"
        if settings.embedding_base_url and settings.embedding_model
        else "disabled; FTS5 fallback active",
    )
    overall = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in checks)
        else ("WARN" if any(item["status"] == "WARN" for item in checks) else "PASS")
    )
    return {"overall": overall, "checks": checks}
