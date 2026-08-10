from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memoryos.evaluation.fixture_agent import _markupsafe_deprecation
from memoryos.evaluation.real_workload_models import DatasetTier, load_real_workload_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_ROOT = PROJECT_ROOT / "benchmarks" / "real_workload" / "public_smoke"


def test_public_smoke_manifest_and_hidden_overlay_are_pinned() -> None:
    manifest = load_real_workload_manifest(SMOKE_ROOT / "manifest.json")
    task = manifest.tasks[0]
    patch = SMOKE_ROOT / "hidden" / str(task.hidden_test.hidden_patch)

    assert manifest.tier is DatasetTier.PUBLIC_REPLAY
    assert manifest.name == "markupsafe-public-smoke"
    assert len(manifest.tasks) == 1
    assert task.source_url is not None
    assert task.source_published_at is not None
    assert task.source_published_at <= task.cutoff
    assert task.solution_commit is not None
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == task.hidden_test.hidden_patch_sha256
    assert task.hidden_test.network == "none"
    assert task.hidden_test.read_only_root is True


def test_deterministic_fixture_applies_only_the_documented_smoke_change(tmp_path: Path) -> None:
    source = tmp_path / "src" / "markupsafe"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text(
        "def __getattr__(name):\n"
        "    import importlib.metadata\n"
        "    import warnings\n"
        "    warnings.warn(\n"
        '        "deprecated",\n'
        "            stacklevel=2,\n"
        "        )\n"
        '        return importlib.metadata.version("markupsafe")\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGES.rst").write_text(
        "Version 3.0.3\n-------------\n\nUnreleased\n\n\nVersion 3.0.2\n",
        encoding="utf-8",
    )

    _markupsafe_deprecation(tmp_path)

    updated = (source / "__init__.py").read_text(encoding="utf-8")
    changes = (tmp_path / "CHANGES.rst").read_text(encoding="utf-8")
    assert "DeprecationWarning,\n            stacklevel=2" in updated
    assert "raises ``DeprecationWarning`` instead of ``UserWarning``" in changes


def test_checked_in_public_smoke_evidence_is_truthful_and_self_consistent() -> None:
    evidence_root = PROJECT_ROOT / "docs" / "verification" / "v2.2" / "markupsafe-public-smoke"
    report_path = evidence_root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    metadata = json.loads((evidence_root / "run-metadata.json").read_text(encoding="utf-8"))
    manifest = load_real_workload_manifest(SMOKE_ROOT / "manifest.json")

    assert report["status"] == "completed"
    assert report["protocol_valid"] is True
    assert report["mode"] == "dry_run"
    assert report["effect_claim"] == "none"
    assert report["runtime"]["evidence_type"] == "deterministic_fixture"
    assert metadata["runtime"]["evidence_type"] == "deterministic_fixture"
    assert report["manifest"]["digest"] == manifest.digest() == metadata["manifest_digest"]
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == metadata["report_sha256"]
    records = {record["condition"]: record for record in report["records"]}
    assert all(record["hidden_test_success"] for record in records.values())
    assert records["no_memory"]["memory_tool_calls"] == 0
    assert records["flat_memory"]["selected_seed_ids"] == [
        "old-warning-category",
        "warning-category-decision",
    ]
    assert records["memoryos"]["selected_seed_ids"] == ["warning-category-decision"]
    assert records["memoryos"]["retrieval_runs"] == 1
    serialized = json.dumps(report)
    assert "C:\\Users" not in serialized
    assert "USE-USERWARNING-STALE" not in serialized
