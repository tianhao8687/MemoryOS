from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memoryos.api import create_app
from memoryos.config import settings_for
from memoryos.security.token import TokenManager


@pytest.mark.v2
def test_v2_http_truth_debug_feedback_and_consolidation_contracts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path / "v2-api-data")
    token = TokenManager(settings.token_path).get_or_create()
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8765",
    }
    memory_payload = {
        "scope_type": "repository",
        "scope_key": "api-v2-repo",
        "memory_type": "project",
        "category": "decision",
        "key": "architecture.backend",
        "title": "Use FastAPI",
        "content": "The backend framework uses FastAPI.",
        "created_by": "manual",
        "activate_immediately": True,
        "source": {
            "source_type": "manual",
            "source_ref": "api:v2",
            "excerpt": "The backend framework uses FastAPI.",
        },
    }
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["version"] == "2.1.0"
        memory = client.post("/api/memories", json=memory_payload, headers=headers).json()["memory"]
        truth = client.post(
            "/api/current-truth",
            json={
                "scope_type": "repository",
                "scope_key": "api-v2-repo",
                "subject": "project.backend_framework",
                "predicate": "uses",
            },
        )
        assert truth.status_code == 200
        assert truth.json()["state"] == "resolved"
        graph = client.post(
            "/api/claim-graph",
            json={
                "scope_key": "api-v2-repo",
                "subject": "project.backend_framework",
                "predicate": "uses",
            },
        ).json()
        assert graph["nodes"][0]["memory_id"] == memory["id"]

        context = client.post(
            "/api/debug/context",
            json={"task": "current backend framework", "repository": "api-v2-repo"},
        ).json()
        assert context["manifest"]
        assert context["retrieval_run_id"]
        feedback = client.post(
            "/api/feedback",
            json={
                "retrieval_run_id": context["retrieval_run_id"],
                "memory_id": memory["id"],
                "helpful": "yes",
                "actor": "api-test",
            },
            headers=headers,
        )
        assert feedback.status_code == 200
        assert feedback.json()["feedback"]["fact_status_changed"] is False

        consolidation = client.post(
            "/api/consolidate",
            json={
                "scope_type": "repository",
                "scope_key": "api-v2-repo",
                "dry_run": True,
            },
            headers=headers,
        )
        assert consolidation.status_code == 200
        assert consolidation.json()["dry_run"] is True
        assert client.get("/api/freshness").json() == []
        benchmark = client.get("/api/benchmarks/memorybench-v2")
        assert benchmark.status_code == 200
        assert benchmark.json()["schema"] == "memorybench-v2-report@1"
        coding_benchmark = client.get("/api/benchmarks/coding-memory-bench-v2.1")
        assert coding_benchmark.status_code == 200
        assert coding_benchmark.json()["schema"] == "coding-memory-bench-v2.1@1"
        assert benchmark.json()["suites"]["agent_ab"]["real_model"]["status"] == "external_blocker"
