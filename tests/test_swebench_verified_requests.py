from __future__ import annotations

import hashlib
import json
import runpy
from datetime import datetime
from pathlib import Path

import pytest

from memoryos.evaluation.real_workload_models import load_real_workload_manifest

ROOT = Path(__file__).parents[1]
TASK_ROOT = ROOT / "benchmarks" / "real_workload" / "swebench_verified" / "requests_6028"


def test_requests_6028_public_replay_assets_are_pinned() -> None:
    manifest = load_real_workload_manifest(TASK_ROOT / "manifest.json")
    provenance = json.loads((TASK_ROOT / "provenance.json").read_bytes())
    task = manifest.tasks[0]
    assert task.hidden_test.hidden_patch is not None
    patch = TASK_ROOT / "hidden" / task.hidden_test.hidden_patch

    assert manifest.name == "requests-proxy-auth-ablation"
    assert provenance["instance_id"] == "psf__requests-6028"
    assert provenance["base_commit"] == task.base_commit
    assert provenance["solution_merge_commit"] == task.solution_commit
    assert datetime.fromisoformat(provenance["issue_created_at"].replace("Z", "+00:00")) <= (
        task.cutoff
    )
    assert datetime.fromisoformat(provenance["solution_merged_at"].replace("Z", "+00:00")) > (
        task.cutoff
    )
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == task.hidden_test.hidden_patch_sha256
    assert provenance["gold_patch_used_by_agent"] is False


def test_requests_6028_hidden_scorer_separates_base_and_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = TASK_ROOT / "hidden" / "requests_proxy_auth_hidden.patch"
    hidden_source = _added_file_source(patch)
    hidden_test = tmp_path / "benchmark_hidden_test.py"
    hidden_test.write_text(hidden_source, encoding="utf-8", newline="\n")
    package = tmp_path / "requests"
    package.mkdir()
    target = package / "utils.py"
    target.write_text(_target_function(fixed=False), encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AssertionError, match="authentication was lost"):
        runpy.run_path(str(hidden_test))

    target.write_text(_target_function(fixed=True), encoding="utf-8", newline="\n")
    runpy.run_path(str(hidden_test))


def _added_file_source(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("@@ ")) + 1
    return "\n".join(line[1:] for line in lines[start:] if line.startswith("+")) + "\n"


def _target_function(*, fixed: bool) -> str:
    auth_restore = "\n    if auth:\n        netloc = '@'.join([auth, netloc])\n" if fixed else ""
    return (
        "def prepend_scheme_if_needed(url, new_scheme):\n"
        "    parsed = parse_url(url)\n"
        "    scheme, auth, host, port, path, query, fragment = parsed\n"
        "    netloc = parsed.netloc\n"
        "    if not netloc:\n"
        "        netloc, path = path, netloc\n"
        "    if scheme is None:\n"
        "        scheme = new_scheme\n"
        "    if path is None:\n"
        "        path = ''\n"
        f"{auth_restore}"
        "    return urlunparse((scheme, netloc, path, '', query, fragment))\n"
    )
