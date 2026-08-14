from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memoryos.api import create_app
from memoryos.config import MemoryOSSettings, settings_for


def test_unwired_staleness_model_is_not_configurable_or_advertised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORYOS_STALENESS_MODEL", "unused-model")
    settings = settings_for(tmp_path / "capability-data")

    assert "staleness_model" not in MemoryOSSettings.model_fields
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/settings")

    assert response.status_code == 200
    assert "staleness_judgement" not in response.json()["provider_capabilities"]
