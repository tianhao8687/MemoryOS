from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from memoryos.evaluation.agent_ab import run_fixture_agent_ab
from memoryos.evaluation.metrics import bootstrap_mean_difference


@dataclass(frozen=True)
class AgentEndpoint:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 60.0


class RealPairedAgentRunner:
    """Run paired, same-model coding decisions with and without MemoryOS context."""

    def __init__(self, endpoint: AgentEndpoint) -> None:
        self.endpoint = endpoint

    def run(self, *, tasks: int = 50) -> dict[str, Any]:
        if tasks < 50:
            raise ValueError("the V2.1 real-agent protocol requires at least 50 paired tasks")
        records = []
        baseline_scores: list[float] = []
        enabled_scores: list[float] = []
        for index in range(tasks):
            task = self._task(index)
            baseline = self._call(task["task_prompt"])
            enabled = self._call(
                task["task_prompt"] + "\n\nMEMORYOS CURRENT TRUTH:\n" + task["memory_context"]
            )
            baseline_score = self._score(baseline, task)
            enabled_score = self._score(enabled, task)
            baseline_scores.append(baseline_score)
            enabled_scores.append(enabled_score)
            records.append(
                {
                    "task_id": task["id"],
                    "task_hash": hashlib.sha256(task["task_prompt"].encode("utf-8")).hexdigest(),
                    "baseline": baseline,
                    "memoryos_enabled": enabled,
                    "baseline_success": bool(baseline_score),
                    "memoryos_success": bool(enabled_score),
                }
            )
        return {
            "status": "completed",
            "evidence_type": "real_model_paired",
            "real_model": True,
            "model": self.endpoint.model,
            "sample_size": tasks,
            "baseline_task_success": sum(baseline_scores) / tasks,
            "memoryos_task_success": sum(enabled_scores) / tasks,
            "paired_difference": bootstrap_mean_difference(
                baseline_scores,
                enabled_scores,
                seed=20260810,
            ),
            "effect_claim": "measured_on_this_authored_paired_protocol_only",
            "records": records,
        }

    def _call(self, prompt: str) -> dict[str, Any]:
        headers = (
            {"Authorization": f"Bearer {self.endpoint.api_key}"} if self.endpoint.api_key else {}
        )
        started = time.perf_counter()
        response = httpx.post(
            f"{self.endpoint.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": self.endpoint.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Act as a coding agent. Return JSON only with framework, dependency, "
                            "repeat_failure, and rationale. Do not claim to inspect unavailable "
                            "data."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=self.endpoint.timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        decoded = json.loads(content)
        if not isinstance(decoded, dict):
            raise ValueError("agent response must be a JSON object")
        return {
            "framework": str(decoded.get("framework", "")),
            "dependency": str(decoded.get("dependency", "")),
            "repeat_failure": bool(decoded.get("repeat_failure", True)),
            "rationale": str(decoded.get("rationale", ""))[:1000],
            "latency_seconds": round(time.perf_counter() - started, 3),
        }

    @staticmethod
    def _score(result: dict[str, Any], task: dict[str, str]) -> float:
        return float(
            result.get("framework", "").lower() == task["framework"].lower()
            and result.get("dependency", "").lower() == task["dependency"].lower()
            and result.get("repeat_failure") is False
        )

    @staticmethod
    def _task(index: int) -> dict[str, str]:
        framework = "FastAPI" if index % 2 == 0 else "Django"
        dependency = "none" if index % 3 else "PostgreSQL"
        failure = "shared mutable cache caused a worker race"
        return {
            "id": f"real-agent-{index + 1:03d}",
            "framework": framework,
            "dependency": dependency,
            "task_prompt": (
                "Choose the repository framework and dependency for a small endpoint, then state "
                "whether to repeat a previously failed concurrency pattern. Repository files are "
                "intentionally neutral, so report uncertainty when no decision evidence is present."
            ),
            "memory_context": (
                f"Confirmed decision: use {framework}. Confirmed dependency choice: {dependency}. "
                f"Known failure: {failure}; do not repeat it."
            ),
        }


def external_blocker_report(*, tasks: int = 50, reason: str) -> dict[str, Any]:
    return {
        "status": "external_blocker",
        "evidence_type": "none",
        "real_model": False,
        "requested_sample_size": tasks,
        "completed_sample_size": 0,
        "reason": reason,
        "effect_claim": "none",
        "fixture": run_fixture_agent_ab(tasks=tasks),
        "truthfulness": "Fixture output validates plumbing only and is not a real-agent result.",
    }


__all__ = ["AgentEndpoint", "RealPairedAgentRunner", "external_blocker_report"]
