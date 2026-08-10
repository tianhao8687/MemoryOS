from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from memoryos.evaluation.real_workload_models import (
    DatasetTier,
    RealWorkloadManifest,
    load_real_workload_manifest,
)

IMAGE = "python:3.12-slim@sha256:" + "a" * 64
BASE = "1" * 40
SOLUTION = "2" * 40
SOURCE = "0" * 40


def manifest_payload(*, tier: str = "public_replay") -> dict[str, object]:
    clone_url = "https://github.com/example/project.git"
    if tier == "harness_fixture":
        clone_url = "fixtures/project"
    return {
        "schema_version": "2.2",
        "name": "public-smoke",
        "tier": tier,
        "generated_at": "2026-08-10T00:00:00Z",
        "repositories": [
            {
                "id": "project",
                "clone_url": clone_url,
                "source_url": "https://github.com/example/project",
                "license_spdx": "MIT",
                "license_url": "https://github.com/example/project/blob/main/LICENSE",
            }
        ],
        "memories": [
            {
                "id": "decision-before-cutoff",
                "repository_id": "project",
                "memory_type": "project",
                "category": "architecture",
                "title": "Use explicit result objects",
                "content": "Confirmed decision: return an explicit Result object.",
                "captured_at": "2025-01-01T00:00:00Z",
                "source_commit": SOURCE,
                "source_ref": "docs/decisions.md",
                "expectation": "helpful",
            }
        ],
        "tasks": [
            {
                "id": "fix-parser",
                "repository_id": "project",
                "sequence_id": "parser-history",
                "sequence_index": 1,
                "base_commit": BASE,
                "solution_commit": SOLUTION,
                "cutoff": "2025-02-01T00:00:00Z",
                "source_url": "https://github.com/example/project/issues/1",
                "source_published_at": "2025-01-31T00:00:00Z",
                "prompt": "Fix the parser error handling without changing its public API.",
                "memory_seed_ids": ["decision-before-cutoff"],
                "hidden_test": {
                    "image": IMAGE,
                    "command": ["python", "-m", "pytest", "-q"],
                },
                "tags": ["python", "parser"],
            }
        ],
    }


def test_public_manifest_is_strict_and_digest_is_stable(tmp_path: Path) -> None:
    payload = manifest_payload()
    manifest = RealWorkloadManifest.model_validate(payload)

    assert manifest.tier is DatasetTier.PUBLIC_REPLAY
    assert manifest.tasks[0].cutoff.isoformat() == "2025-02-01T00:00:00+00:00"
    assert len(manifest.digest()) == 64
    assert manifest.digest() == RealWorkloadManifest.model_validate(payload).digest()

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_real_workload_manifest(path).digest() == manifest.digest()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["tasks"][0].update({"cutoff": "2025-02-01T00:00:00"}),
            "explicit timezone",
        ),
        (
            lambda data: data["tasks"][0].update({"source_published_at": "2025-02-02T00:00:00Z"}),
            "published after",
        ),
        (
            lambda data: data["tasks"][0].pop("source_published_at"),
            "source_published_at",
        ),
        (
            lambda data: data["tasks"][0]["hidden_test"].update({"image": "python:3.12-slim"}),
            "sha256 digest",
        ),
        (
            lambda data: data["tasks"][0]["hidden_test"].update(
                {"command": ["sh", "-c", "pytest"]}
            ),
            "direct argv",
        ),
        (
            lambda data: data["tasks"][0]["hidden_test"].update(
                {"hidden_patch": "../secret.patch", "hidden_patch_sha256": "a" * 64}
            ),
            "traversal-free",
        ),
        (
            lambda data: data["memories"][0].update({"captured_at": "2025-03-01T00:00:00Z"}),
            "captured after",
        ),
        (
            lambda data: data["repositories"][0].update({"clone_url": "file:///tmp/project"}),
            "must use https",
        ),
        (
            lambda data: data["repositories"][0].update(
                {"clone_url": "https://token@example.com/project.git"}
            ),
            "must not embed credentials",
        ),
        (
            lambda data: data["tasks"][0].update({"prompt": f"Copy solution {SOLUTION}"}),
            "hidden solution commit",
        ),
    ],
)
def test_manifest_rejects_leaks_and_unreproducible_inputs(mutate: object, message: str) -> None:
    payload = deepcopy(manifest_payload())
    assert callable(mutate)
    mutate(payload)
    with pytest.raises(ValidationError, match=message):
        RealWorkloadManifest.model_validate(payload)


def test_private_dataset_requires_consent_record() -> None:
    payload = manifest_payload(tier="private_opt_in")
    with pytest.raises(ValidationError, match="consent_record"):
        RealWorkloadManifest.model_validate(payload)


def test_fixture_may_use_local_repository_but_remains_labelled() -> None:
    payload = manifest_payload(tier="harness_fixture")
    payload["tasks"][0].pop("solution_commit")
    payload["tasks"][0].pop("source_url")
    payload["tasks"][0].pop("source_published_at")
    payload["memories"][0].pop("source_commit")

    manifest = RealWorkloadManifest.model_validate(payload)

    assert manifest.tier is DatasetTier.HARNESS_FIXTURE


def test_cross_project_guard_requires_embedded_canary() -> None:
    payload = manifest_payload()
    memory = payload["memories"][0]
    memory["repository_id"] = "project"
    memory["expectation"] = "cross_project_guard"
    with pytest.raises(ValidationError, match="require a canary"):
        RealWorkloadManifest.model_validate(payload)
