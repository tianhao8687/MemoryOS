from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RelativeUsageArm:
    condition: str
    usage_file: Path
    attempt_file: Path
    terminal_file: Path
    guard_file: Path

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RelativeUsageArm:
        condition = str(value["condition"])
        if not condition or any(character in condition for character in "\x00\r\n"):
            raise ValueError("relative-usage condition is invalid")
        return cls(
            condition=condition,
            usage_file=Path(value["usage_file"]).resolve(),
            attempt_file=Path(value["attempt_file"]).resolve(),
            terminal_file=Path(value["terminal_file"]).resolve(),
            guard_file=Path(value["guard_file"]).resolve(),
        )


@dataclass(frozen=True)
class RelativeUsageSnapshot:
    condition: str
    terminal: bool
    input_tokens: int
    provider_attempts: int
    completed_responses: int
    cost_usd: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "terminal": self.terminal,
            "input_tokens": self.input_tokens,
            "provider_attempts": self.provider_attempts,
            "completed_responses": self.completed_responses,
            "cost_usd": str(self.cost_usd),
        }


class RelativeUsageGuardController:
    """Stop a trailing A/B/C arm before its next provider dispatch.

    Comparisons become actionable only after both peers have reached their agent
    terminal marker. This avoids treating normal parallel scheduling skew as model
    overuse. The provider hook checks the generated guard file synchronously.
    """

    def __init__(
        self,
        arms: tuple[RelativeUsageArm, ...],
        *,
        multiplier: Decimal = Decimal("1.30"),
    ) -> None:
        if len(arms) != 3:
            raise ValueError("relative usage guard requires exactly three A/B/C arms")
        if len({arm.condition for arm in arms}) != len(arms):
            raise ValueError("relative usage guard conditions must be unique")
        if multiplier <= Decimal("1"):
            raise ValueError("relative usage multiplier must be greater than one")
        self.arms = arms
        self.multiplier = multiplier
        self.decisions: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_config(cls, value: dict[str, Any]) -> RelativeUsageGuardController:
        if value.get("schema_version") != "1.0":
            raise ValueError("unsupported relative usage guard schema")
        try:
            multiplier = Decimal(str(value.get("multiplier", "1.30")))
        except InvalidOperation as exc:
            raise ValueError("relative usage multiplier is invalid") from exc
        return cls(
            tuple(RelativeUsageArm.from_dict(item) for item in value["arms"]),
            multiplier=multiplier,
        )

    def initialize(self) -> None:
        for arm in self.arms:
            _atomic_write_json(
                arm.guard_file,
                {
                    "schema_version": "1.0",
                    "stop": False,
                    "condition": arm.condition,
                    "reason": "relative usage guard armed",
                },
            )

    def snapshot(self) -> dict[str, RelativeUsageSnapshot]:
        return {arm.condition: _snapshot_arm(arm) for arm in self.arms}

    def evaluate(self) -> dict[str, RelativeUsageSnapshot]:
        snapshots = self.snapshot()
        for arm in self.arms:
            target = snapshots[arm.condition]
            if target.terminal or arm.condition in self.decisions:
                continue
            peers = [
                snapshots[other.condition]
                for other in self.arms
                if other.condition != arm.condition
            ]
            if not all(peer.terminal for peer in peers):
                continue
            exceeded = _first_exceeded_metric(target, peers, self.multiplier)
            if exceeded is None:
                continue
            metric, observed, ceiling = exceeded
            decision = {
                "schema_version": "1.0",
                "stop": True,
                "condition": arm.condition,
                "reason": f"relative_overuse:{metric}",
                "decided_at": datetime.now(UTC).isoformat(),
                "multiplier": str(self.multiplier),
                "metric": metric,
                "observed": str(observed),
                "ceiling": str(ceiling),
                "target": target.as_dict(),
                "terminal_peers": [peer.as_dict() for peer in peers],
            }
            _atomic_write_json(arm.guard_file, decision)
            self.decisions[arm.condition] = decision
        return snapshots

    def run(self, *, poll_seconds: float = 0.25, timeout_seconds: float = 7200) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.monotonic()
        while True:
            snapshots = self.evaluate()
            if all(snapshot.terminal for snapshot in snapshots.values()):
                return self.report(snapshots, status="completed")
            if time.monotonic() - started >= timeout_seconds:
                return self.report(snapshots, status="timeout")
            time.sleep(poll_seconds)

    def report(
        self,
        snapshots: dict[str, RelativeUsageSnapshot] | None = None,
        *,
        status: str,
    ) -> dict[str, Any]:
        current = snapshots or self.snapshot()
        return {
            "schema_version": "1.0",
            "status": status,
            "multiplier": str(self.multiplier),
            "comparison_policy": "both_peers_terminal_then_stop_before_next_dispatch",
            "metrics": ["input_tokens", "provider_attempts", "cost_usd"],
            "arms": {name: item.as_dict() for name, item in current.items()},
            "decisions": self.decisions,
        }


def _snapshot_arm(arm: RelativeUsageArm) -> RelativeUsageSnapshot:
    usage = _read_complete_jsonl(arm.usage_file)
    attempts = _read_complete_jsonl(arm.attempt_file)
    cost = Decimal("0")
    input_tokens = 0
    for record in usage:
        input_tokens += int(record.get("input_tokens", 0))
        value = record.get("cost_usd")
        if value is not None:
            cost += Decimal(str(value))
    return RelativeUsageSnapshot(
        condition=arm.condition,
        terminal=arm.terminal_file.is_file(),
        input_tokens=input_tokens,
        provider_attempts=len(attempts),
        completed_responses=len(usage),
        cost_usd=cost,
    )


def _read_complete_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if text and not text.endswith(("\n", "\r")):
        lines = lines[:-1]
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid guard JSONL at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"guard JSONL row must be an object at {path}:{line_number}")
        records.append(value)
    return records


def _first_exceeded_metric(
    target: RelativeUsageSnapshot,
    peers: list[RelativeUsageSnapshot],
    multiplier: Decimal,
) -> tuple[str, Decimal, Decimal] | None:
    values = (
        (
            "input_tokens",
            Decimal(target.input_tokens),
            [Decimal(item.input_tokens) for item in peers],
        ),
        (
            "provider_attempts",
            Decimal(target.provider_attempts),
            [Decimal(item.provider_attempts) for item in peers],
        ),
        ("cost_usd", target.cost_usd, [item.cost_usd for item in peers]),
    )
    for metric, observed, peer_values in values:
        ceiling = max(peer_values) * multiplier
        if observed > ceiling:
            return metric, observed, ceiling
    return None


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


__all__ = [
    "RelativeUsageArm",
    "RelativeUsageGuardController",
    "RelativeUsageSnapshot",
]
