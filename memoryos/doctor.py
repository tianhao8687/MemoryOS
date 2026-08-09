from __future__ import annotations

import os
from importlib import import_module
from typing import Any

from sqlalchemy import select, text

from memoryos.config import MemoryOSSettings
from memoryos.db.models import AnnIndexStateRow
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
    try:
        module = import_module("sqlite_vec")
        version = str(getattr(module, "__version__", "bundled"))
        add("sqlite_vec_runtime", "PASS", version)
    except (ImportError, OSError) as exc:
        add("sqlite_vec_runtime", "WARN", f"unavailable; exact fallback active: {exc}")
    try:
        with database.session() as session:
            vector_rows = list(session.scalars(select(AnnIndexStateRow)))
        if not settings.embedding_base_url or not settings.embedding_model:
            add("vector_index", "WARN", "embedding provider disabled; FTS5 remains active")
        elif not vector_rows:
            add("vector_index", "WARN", "not built; run `memoryos vector-rebuild`")
        elif any(row.status == "unavailable" for row in vector_rows):
            reasons = "; ".join(
                sorted({row.unavailable_reason or "unknown" for row in vector_rows})
            )
            add("vector_index", "WARN", f"sqlite-vec unavailable; exact fallback active: {reasons}")
        else:
            add(
                "vector_index",
                "PASS",
                f"sqlite-vec live path ready ({sum(row.item_count for row in vector_rows)} items)",
            )
    except Exception as exc:  # pragma: no cover - diagnostic boundary
        add("vector_index", "FAIL", str(exc))
    overall = (
        "FAIL"
        if any(item["status"] == "FAIL" for item in checks)
        else ("WARN" if any(item["status"] == "WARN" for item in checks) else "PASS")
    )
    return {"overall": overall, "checks": checks}
