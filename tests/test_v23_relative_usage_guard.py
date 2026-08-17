from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from memoryos.evaluation.relative_usage_guard import (
    RelativeUsageArm,
    RelativeUsageGuardController,
)


def _arm(root: Path, condition: str) -> RelativeUsageArm:
    directory = root / condition
    directory.mkdir(parents=True)
    return RelativeUsageArm(
        condition=condition,
        usage_file=directory / "provider-usage.jsonl",
        attempt_file=directory / "provider-attempts.jsonl",
        terminal_file=directory / "run-stderr.log",
        guard_file=directory / "usage-guard.json",
    )


def _set_usage(
    arm: RelativeUsageArm,
    *,
    input_tokens: int,
    attempts: int,
    cost_usd: str,
    terminal: bool,
) -> None:
    arm.usage_file.write_text(
        json.dumps({"input_tokens": input_tokens, "cost_usd": cost_usd}) + "\n",
        encoding="utf-8",
    )
    arm.attempt_file.write_text(
        "".join(json.dumps({"attempt": index}) + "\n" for index in range(attempts)),
        encoding="utf-8",
    )
    if terminal:
        arm.terminal_file.write_text("done\n", encoding="utf-8")


@pytest.mark.v23
def test_relative_guard_waits_for_both_terminal_peers(tmp_path: Path) -> None:
    arms = tuple(_arm(tmp_path, name) for name in ("a", "b", "c"))
    controller = RelativeUsageGuardController(arms)
    controller.initialize()
    _set_usage(arms[0], input_tokens=100, attempts=10, cost_usd="1", terminal=True)
    _set_usage(arms[1], input_tokens=1_000, attempts=11, cost_usd="1", terminal=False)
    _set_usage(arms[2], input_tokens=100, attempts=10, cost_usd="1", terminal=False)

    controller.evaluate()

    assert controller.decisions == {}
    assert json.loads(arms[1].guard_file.read_text(encoding="utf-8"))["stop"] is False


@pytest.mark.v23
def test_relative_guard_stops_strictly_above_thirty_percent_before_next_call(
    tmp_path: Path,
) -> None:
    arms = tuple(_arm(tmp_path, name) for name in ("a", "b", "c"))
    controller = RelativeUsageGuardController(arms, multiplier=Decimal("1.30"))
    controller.initialize()
    _set_usage(arms[0], input_tokens=100, attempts=10, cost_usd="1", terminal=True)
    _set_usage(arms[1], input_tokens=131, attempts=10, cost_usd="1", terminal=False)
    _set_usage(arms[2], input_tokens=100, attempts=10, cost_usd="1", terminal=True)

    snapshots = controller.evaluate()

    assert snapshots["b"].input_tokens == 131
    decision = controller.decisions["b"]
    assert decision["metric"] == "input_tokens"
    assert decision["observed"] == "131"
    assert decision["ceiling"] == "130.00"
    guard = json.loads(arms[1].guard_file.read_text(encoding="utf-8"))
    assert guard["stop"] is True
    assert guard["reason"] == "relative_overuse:input_tokens"


@pytest.mark.v23
def test_relative_guard_does_not_stop_at_exact_ceiling(tmp_path: Path) -> None:
    arms = tuple(_arm(tmp_path, name) for name in ("a", "b", "c"))
    controller = RelativeUsageGuardController(arms)
    controller.initialize()
    _set_usage(arms[0], input_tokens=100, attempts=10, cost_usd="1", terminal=True)
    _set_usage(arms[1], input_tokens=130, attempts=13, cost_usd="1.30", terminal=False)
    _set_usage(arms[2], input_tokens=100, attempts=10, cost_usd="1", terminal=True)

    controller.evaluate()

    assert controller.decisions == {}
