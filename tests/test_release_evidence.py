from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts.acceptance_v21 import REQUIRED_VERIFICATION_STEPS, _runtime_verified
from scripts.benchmark_v21_pipeline import _percentile


def _verification_report(commit: str = "a" * 40) -> dict[str, Any]:
    return {
        "schema": "memoryos-v2.1-verification@1",
        "result": "RUNNING",
        "branch": "main",
        "started_commit": commit,
        "git_dirty_before_run": False,
        "steps": [{"label": label, "exit_code": 0} for label in REQUIRED_VERIFICATION_STEPS],
    }


@pytest.mark.v21
def test_acceptance_requires_current_complete_clean_verification_run() -> None:
    commit = "a" * 40
    valid = _verification_report(commit)
    assert _runtime_verified(valid, commit)

    stale = deepcopy(valid)
    stale["started_commit"] = "b" * 40
    assert not _runtime_verified(stale, commit)

    dirty = deepcopy(valid)
    dirty["git_dirty_before_run"] = True
    assert not _runtime_verified(dirty, commit)

    incomplete = deepcopy(valid)
    incomplete["steps"] = incomplete["steps"][:-1]
    assert not _runtime_verified(incomplete, commit)

    failed = deepcopy(valid)
    failed["steps"][4]["exit_code"] = 1
    assert not _runtime_verified(failed, commit)

    reordered = deepcopy(valid)
    reordered["steps"][0], reordered["steps"][1] = (
        reordered["steps"][1],
        reordered["steps"][0],
    )
    assert not _runtime_verified(reordered, commit)


@pytest.mark.v21
def test_performance_gate_uses_nearest_rank_p95() -> None:
    samples = [float(value) for value in range(25)]
    assert _percentile(samples, 0.95) == 23.0
    assert _percentile(samples, 0.0) == 0.0
    assert _percentile(samples, 1.0) == 24.0
    with pytest.raises(ValueError, match="at least one"):
        _percentile([], 0.95)
    with pytest.raises(ValueError, match="between 0 and 1"):
        _percentile(samples, 1.1)
