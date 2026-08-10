from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from memoryos.api import create_app
from memoryos.config import settings_for
from memoryos.security.logging import configure_logging
from memoryos.security.token import TokenManager


class InvalidProviderResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": "not-json"}}]}


def test_non_loopback_bind_address_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="only loopback bind addresses are permitted"):
        settings_for(tmp_path / "unsafe-data", host="0.0.0.0")  # noqa: S104
    with pytest.raises(ValidationError, match="only loopback bind addresses are permitted"):
        settings_for(tmp_path / "unsafe-data", host="192.168.1.10")
    settings = settings_for(tmp_path / "safe-data")
    with pytest.raises(ValidationError, match="only loopback bind addresses are permitted"):
        settings.host = "0.0.0.0"  # noqa: S104


def test_unsafe_or_portless_allowed_origins_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="explicit loopback"):
        settings_for(tmp_path, allowed_origins=["https://example.invalid:443"])
    with pytest.raises(ValidationError, match="explicit loopback"):
        settings_for(tmp_path, allowed_origins=["http://127.0.0.1"])
    with pytest.raises(ValidationError, match="explicit loopback"):
        settings_for(tmp_path, allowed_origins=["http://127.0.0.1:8765/"])
    configured = settings_for(tmp_path, allowed_origins=["http://localhost:4321"])
    assert configured.allowed_origins == ["http://localhost:4321"]


def test_runtime_resource_settings_are_bounded(tmp_path: Path) -> None:
    for overrides in (
        {"port": 0},
        {"busy_timeout_ms": -1},
        {"source_excerpt_limit": -1},
        {"provider_timeout_seconds": 0},
        {"provider_max_input_chars": -1},
        {"log_level": "verbose"},
    ):
        with pytest.raises(ValidationError):
            settings_for(tmp_path, **overrides)


def test_structured_logging_redacts_secrets(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "logging-data")
    configure_logging(settings)
    memoryos_logger = logging.getLogger("memoryos.security-test")
    memoryos_logger.warning("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
    root = logging.getLogger("memoryos")
    try:
        for handler in root.handlers:
            handler.flush()
        payload = json.loads((settings.log_dir / "memoryos.log").read_text(encoding="utf-8"))
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "memoryos.security-test"
        assert "abcdefghijklmnopqrstuvwxyz" not in payload["message"]
        assert "[REDACTED:bearer]" in payload["message"]
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()


def _payload() -> dict[str, object]:
    return {
        "scope_type": "repository",
        "scope_key": "api-repo",
        "memory_type": "project",
        "category": "decision",
        "key": "architecture.api",
        "title": "Use FastAPI",
        "content": "Use FastAPI for the local HTTP API.",
        "created_by": "agent",
        "source": {
            "source_type": "agent",
            "source_ref": "api:test",
            "excerpt": "Use FastAPI for the local HTTP API.",
        },
    }


def test_write_auth_origin_and_api_lifecycle(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "api-data")
    app = create_app(settings)
    token = TokenManager(settings.token_path).get_or_create()
    with TestClient(app) as client:
        assert client.post("/api/memories", json=_payload()).status_code == 401
        rejected = client.post(
            "/api/memories",
            json=_payload(),
            headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"},
        )
        assert rejected.status_code == 403

        created = client.post(
            "/api/memories",
            json=_payload(),
            headers={"Authorization": f"Bearer {token}", "Origin": "http://127.0.0.1:8765"},
        )
        assert created.status_code == 200
        memory_id = created.json()["memory"]["id"]
        assert created.json()["memory"]["status"] == "candidate"

        confirmed = client.post(
            f"/api/memories/{memory_id}/confirm",
            json={},
            headers={"Authorization": f"Bearer {token}", "Origin": "http://localhost:8765"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["memory"]["status"] == "active"
        assert client.get(f"/api/memories/{memory_id}/explain").json()["sources"]
        assert (
            client.get(
                "/api/memories",
                params={"q": "FastAPI"},
                headers={"Authorization": f"Bearer {token}"},
            ).json()["total"]
            == 1
        )
        assert client.get("/api/health").json()["ok"] is True


def test_bundled_ui_cookie_is_valid_for_same_origin_writes(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "cookie-data")
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        payload = _payload()
        payload["created_by"] = "manual"
        payload["activate_immediately"] = True
        response = client.post(
            "/api/memories", json=payload, headers={"Origin": "http://127.0.0.1:8765"}
        )
        assert response.status_code == 200
        assert response.json()["memory"]["status"] == "active"


def test_configured_extractor_failure_returns_502_without_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(
        tmp_path / "provider-data",
        extractor_base_url="http://provider.invalid",
        extractor_model="test",
    )
    token = TokenManager(settings.token_path).get_or_create()
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: InvalidProviderResponse())
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/extract",
            json={
                "text": "We decided to use FastAPI.",
                "scope_type": "repository",
                "scope_key": "api-repo",
                "source_ref": "api:provider-test",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "PROVIDER_FAILURE"
        assert (
            client.get("/api/memories", headers={"Authorization": f"Bearer {token}"}).json()[
                "total"
            ]
            == 0
        )


def test_api_conflict_resolution_and_backup(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "conflict-data")
    token = TokenManager(settings.token_path).get_or_create()
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8765",
    }
    current_payload = _payload()
    current_payload["created_by"] = "manual"
    current_payload["activate_immediately"] = True
    candidate_payload = _payload()
    candidate_payload["title"] = "Use Django"
    candidate_payload["content"] = "Use Django instead of FastAPI."
    candidate_payload["source"] = {
        "source_type": "agent",
        "source_ref": "api:conflict",
        "excerpt": "Use Django instead of FastAPI.",
    }

    with TestClient(create_app(settings)) as client:
        current = client.post("/api/memories", json=current_payload, headers=headers).json()[
            "memory"
        ]
        candidate = client.post("/api/memories", json=candidate_payload, headers=headers).json()[
            "memory"
        ]
        unresolved = client.post(
            f"/api/memories/{candidate['id']}/confirm", json={}, headers=headers
        )
        assert unresolved.status_code == 409
        assert unresolved.json()["error"]["code"] == "CONFLICT_DETECTED"
        assert len(client.get("/api/conflicts", headers=headers).json()) == 1

        resolved = client.post(
            f"/api/conflicts/{candidate['id']}/resolve",
            json={"strategy": "supersede", "rationale": "Approved architecture change"},
            headers=headers,
        )
        assert resolved.status_code == 200
        assert resolved.json()["memory"]["status"] == "active"
        assert (
            client.get(f"/api/memories/{current['id']}", headers=headers).json()["status"]
            == "superseded"
        )

        backup = client.post("/api/backup", headers=headers)
        assert backup.status_code == 200
        assert Path(backup.json()["path"]).is_file()
