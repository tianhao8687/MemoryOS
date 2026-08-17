from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from memoryos.evaluation.context_efficiency import ContextEfficiencyRecord
from memoryos.evaluation.openai_compatible_coding_agent import ToolEvent
from memoryos.evaluation.provider_usage import ProviderUsageRecord, aggregate_usage


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    EXTERNAL_BLOCKER = "external_blocker"


class StructuredFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    exception_type: str | None = None
    message: str = Field(max_length=2000)
    message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def create(
        cls,
        code: str,
        message: str,
        *,
        exception_type: str | None = None,
    ) -> StructuredFailure:
        bounded = message.encode("utf-8", errors="replace")[:2000].decode("utf-8", errors="ignore")
        return cls(
            code=code,
            exception_type=exception_type,
            message=bounded,
            message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        )


class ContextEfficiencyRunRecord(BaseModel):
    """One task/condition/cache-phase execution plus its canonical V2.3 record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    cache_phase: Literal["cold", "warm"]
    status: ExecutionStatus
    record: ContextEfficiencyRecord
    failure: StructuredFailure | None = None
    agent_steps: int = Field(default=0, ge=0)
    provider_requests: int = Field(default=0, ge=0)
    provider_attempts: int = Field(default=0, ge=0)
    tests_run: int = Field(default=0, ge=0)
    patches_applied: int = Field(default=0, ge=0)
    patch_path: str
    test_result_path: str
    failure_path: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.record.task_id, self.record.condition.value, self.cache_phase


def build_context_efficiency_summary(
    records: list[ContextEfficiencyRunRecord],
    usage: list[ProviderUsageRecord],
    tool_events: list[ToolEvent],
    *,
    evidence_type: Literal["real_coding_agent", "deterministic_fixture"],
) -> dict[str, Any]:
    usage_by_key: dict[tuple[str, str, str], list[ProviderUsageRecord]] = defaultdict(list)
    for request in usage:
        usage_by_key[(request.task_id, request.condition, request.cache_phase.value)].append(
            request
        )
    events_by_key: dict[tuple[str, str, str], list[ToolEvent]] = defaultdict(list)
    for event in tool_events:
        events_by_key[(event.task_id, event.condition, event.cache_phase.value)].append(event)

    grouped: dict[str, dict[str, Any]] = {}
    conditions = sorted({item.record.condition.value for item in records})
    phases = [phase for phase in ("cold", "warm") if any(r.cache_phase == phase for r in records)]
    for condition in conditions:
        grouped[condition] = {}
        for phase in phases:
            selected = [
                item
                for item in records
                if item.record.condition.value == condition and item.cache_phase == phase
            ]
            request_records = [
                request for item in selected for request in usage_by_key.get(item.key, [])
            ]
            event_records = [
                event for item in selected for event in events_by_key.get(item.key, [])
            ]
            totals = aggregate_usage(request_records)
            successful = sum(item.record.functional_success for item in selected)
            grouped[condition][phase] = {
                "runs": len(selected),
                "completed_runs": sum(
                    item.status is ExecutionStatus.COMPLETED for item in selected
                ),
                "external_blockers": sum(
                    item.status is ExecutionStatus.EXTERNAL_BLOCKER for item in selected
                ),
                "functional_successes": successful,
                "success_rate": successful / len(selected) if selected else None,
                "provider": totals.model_dump(mode="json"),
                "provider_attempts": sum(item.provider_attempts for item in selected),
                "cache_hit_rate": totals.cache_hit_rate,
                "mean_ttft_seconds": _mean_optional(
                    [item.ttft_seconds for item in request_records]
                ),
                "total_run_latency_seconds": round(
                    sum(item.record.latency_seconds for item in selected), 6
                ),
                "progressive_explain_calls": sum(
                    event.tool == "memory_explain" and event.ok for event in event_records
                ),
                "delta_hits": sum(item.record.delta_hits for item in selected),
                "full_fallbacks": sum(item.record.full_fallbacks for item in selected),
                "context_rebases": sum(item.record.context_rebases for item in selected),
                "safety": {
                    "constraint_loss": sum(item.record.constraint_loss for item in selected),
                    "contested_bundle_split": sum(
                        item.record.contested_bundle_split for item in selected
                    ),
                    "stale_memory_uses": sum(item.record.stale_memory_uses for item in selected),
                    "cross_project_leaks": sum(
                        item.record.cross_project_leaks for item in selected
                    ),
                    "blocked_actions": sum(item.record.blocked_actions for item in selected),
                },
            }

    failures = [
        {
            "task_id": item.record.task_id,
            "condition": item.record.condition.value,
            "cache_phase": item.cache_phase,
            "status": item.status.value,
            "code": item.failure.code if item.failure is not None else "functional_failure",
            "failure_path": item.failure_path,
        }
        for item in records
        if not item.record.functional_success
    ]
    external_blockers = sum(item.status is ExecutionStatus.EXTERNAL_BLOCKER for item in records)
    before_after = _build_before_after_comparison(records, usage, tool_events)
    fixture = evidence_type == "deterministic_fixture"
    if records and external_blockers == len(records):
        status = "external_blocker"
        truthfulness = (
            "No benchmark run was executed because the external environment blocked it."
            if fixture
            else "No live provider run was executed because the external environment blocked it."
        )
    elif fixture:
        status = "completed_fixture_with_failures" if failures else "completed_fixture"
        truthfulness = (
            "Deterministic fixture evidence validates execution contracts only; it is not real "
            "provider performance or cost evidence."
        )
    else:
        status = "completed_with_failures" if failures else "completed"
        truthfulness = (
            "Metrics contain only usage returned by the configured provider or tokenizer."
        )
    return {
        "schema_version": "1.0",
        "status": status,
        "evidence_type": evidence_type,
        "truthfulness": truthfulness,
        "run_count": len(records),
        "external_blocker_count": external_blockers,
        "conditions": grouped,
        "before_after": before_after,
        "failures": failures,
    }


def render_context_efficiency_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# MemoryOS V2.3 Context Efficiency",
        "",
        f"状态: `{summary['status']}`",
        "",
        str(summary["truthfulness"]),
        "",
        "| 条件 | 阶段 | 成功率 | 输入 Token | Miss | Hit | 输出 Token | "
        "命中率 | 成本 USD | 平均 TTFT | 总延迟 | Explain | Delta / Full |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: |",
    ]
    conditions = summary.get("conditions", {})
    for condition, phases in conditions.items():
        for phase, metrics in phases.items():
            provider = metrics["provider"]
            runs = int(metrics["runs"])
            successes = int(metrics["functional_successes"])
            rate = _percent(metrics["success_rate"])
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{condition}`",
                        f"`{phase}`",
                        f"{successes}/{runs} ({rate})",
                        str(provider["input_tokens"]),
                        _display(provider["cache_miss_tokens"]),
                        _display(provider["cache_hit_tokens"]),
                        str(provider["output_tokens"]),
                        _percent(metrics["cache_hit_rate"]),
                        _display(provider["cost_usd"]),
                        _display(metrics["mean_ttft_seconds"]),
                        _display(metrics["total_run_latency_seconds"]),
                        str(metrics["progressive_explain_calls"]),
                        f"{metrics['delta_hits']} / {metrics['full_fallbacks']}",
                    ]
                )
                + " |"
            )
    before_after = summary.get("before_after")
    if isinstance(before_after, dict) and before_after.get("comparisons"):
        lines.extend(
            [
                "",
                "## 使用前 / 使用后配对对比",
                "",
                "`no_memory` 是使用前基线; 每个使用后实验臂均使用相同模型、任务文本、"
                "系统提示、起始提交、工作区工具和隐藏测试。",
                "",
                "| 使用后条件 | 阶段 | 配对 | 成功率 (前 → 后) | 输入 Token (前 → 后) | "
                "输出 Token (前 → 后) | 成功请求 (前 → 后) | HTTP 尝试 (前 → 后) | "
                "平均 TTFT (前 → 后) | "
                "总延迟 (前 → 后) | 改善 / 退化 |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for treatment, phases in before_after["comparisons"].items():
            for phase, metrics in phases.items():
                success = metrics["success"]
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            f"`{treatment}`",
                            f"`{phase}`",
                            str(metrics["paired_tasks"]),
                            _before_after_display(success, percent=True),
                            _before_after_display(metrics["input_tokens"]),
                            _before_after_display(metrics["output_tokens"]),
                            _before_after_display(metrics["provider_requests"]),
                            _before_after_display(metrics["provider_attempts"]),
                            _before_after_display(metrics["mean_ttft_seconds"], digits=6),
                            _before_after_display(metrics["total_run_latency_seconds"], digits=6),
                            f"{success['improved_tasks']} / {success['regressed_tasks']}",
                        ]
                    )
                    + " |"
                )
        integrity = before_after.get("integrity", {})
        lines.extend(
            [
                "",
                "配对完整性: "
                f"prompt={integrity.get('identical_prompt_pairs', 0)}/"
                f"{integrity.get('pair_count', 0)}, "
                f"starting-state={integrity.get('identical_starting_state_pairs', 0)}/"
                f"{integrity.get('pair_count', 0)}, "
                f"runtime={integrity.get('identical_runtime_pairs', 0)}/"
                f"{integrity.get('pair_count', 0)}; "
                f"使用前 MemoryOS 调用={integrity.get('baseline_memory_tool_calls', 0)}。",
            ]
        )
    failures = summary.get("failures", [])
    lines.extend(["", "## 失败任务", ""])
    if not failures:
        lines.append("无。")
    else:
        for failure in failures:
            label = (
                f"{failure['task_id']} / {failure['condition']} / {failure['cache_phase']} "
                f"— `{failure['code']}`"
            )
            path = failure.get("failure_path")
            lines.append(f"- [{label}]({path})" if path else f"- {label}")
    lines.extend(["", "## 安全事件", ""])
    safety_totals: dict[str, int] = defaultdict(int)
    for phases in conditions.values():
        for metrics in phases.values():
            for key, value in metrics["safety"].items():
                safety_totals[key] += int(value)
    if not safety_totals:
        lines.append("无可报告执行。")
    else:
        for key, value in sorted(safety_totals.items()):
            lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def _mean_optional(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    concrete = [value for value in values if value is not None]
    return round(sum(concrete) / len(concrete), 6)


def _build_before_after_comparison(
    records: list[ContextEfficiencyRunRecord],
    usage: list[ProviderUsageRecord],
    tool_events: list[ToolEvent],
) -> dict[str, Any] | None:
    baseline_name = "no_memory"
    if not any(item.record.condition.value == baseline_name for item in records):
        return None
    usage_by_key: dict[tuple[str, str, str], list[ProviderUsageRecord]] = defaultdict(list)
    for request in usage:
        usage_by_key[(request.task_id, request.condition, request.cache_phase.value)].append(
            request
        )
    events_by_key: dict[tuple[str, str, str], list[ToolEvent]] = defaultdict(list)
    for event in tool_events:
        events_by_key[(event.task_id, event.condition, event.cache_phase.value)].append(event)

    phases = [phase for phase in ("cold", "warm") if any(r.cache_phase == phase for r in records)]
    treatments = sorted(
        {
            item.record.condition.value
            for item in records
            if item.record.condition.value != baseline_name
        }
    )
    comparisons: dict[str, dict[str, Any]] = {}
    integrity_totals = {
        "pair_count": 0,
        "identical_prompt_pairs": 0,
        "identical_starting_state_pairs": 0,
        "identical_runtime_pairs": 0,
        "baseline_memory_tool_calls": 0,
        "treatment_memory_tool_calls": 0,
    }
    for treatment in treatments:
        comparisons[treatment] = {}
        for phase in phases:
            before_by_task = {
                item.record.task_id: item
                for item in records
                if item.record.condition.value == baseline_name and item.cache_phase == phase
            }
            after_by_task = {
                item.record.task_id: item
                for item in records
                if item.record.condition.value == treatment and item.cache_phase == phase
            }
            task_ids = sorted(set(before_by_task) & set(after_by_task))
            if not task_ids:
                continue
            pairs = [(before_by_task[task_id], after_by_task[task_id]) for task_id in task_ids]
            before_usage = [
                request
                for task_id in task_ids
                for request in usage_by_key.get((task_id, baseline_name, phase), [])
            ]
            after_usage = [
                request
                for task_id in task_ids
                for request in usage_by_key.get((task_id, treatment, phase), [])
            ]
            before_events = [
                event
                for task_id in task_ids
                for event in events_by_key.get((task_id, baseline_name, phase), [])
            ]
            after_events = [
                event
                for task_id in task_ids
                for event in events_by_key.get((task_id, treatment, phase), [])
            ]
            before_totals = aggregate_usage(before_usage)
            after_totals = aggregate_usage(after_usage)
            before_success = [item.record.functional_success for item, _ in pairs]
            after_success = [item.record.functional_success for _, item in pairs]
            before_rate = sum(before_success) / len(pairs)
            after_rate = sum(after_success) / len(pairs)
            baseline_memory_calls = sum(event.category == "memory" for event in before_events)
            treatment_memory_calls = sum(event.category == "memory" for event in after_events)
            prompt_matches = sum(
                before.record.prompt_sha256 == after.record.prompt_sha256 for before, after in pairs
            )
            state_matches = sum(
                before.record.starting_state_sha256 == after.record.starting_state_sha256
                for before, after in pairs
            )
            runtime_matches = sum(
                before.record.runtime_sha256 == after.record.runtime_sha256
                for before, after in pairs
            )
            integrity_totals["pair_count"] += len(pairs)
            integrity_totals["identical_prompt_pairs"] += prompt_matches
            integrity_totals["identical_starting_state_pairs"] += state_matches
            integrity_totals["identical_runtime_pairs"] += runtime_matches
            integrity_totals["baseline_memory_tool_calls"] += baseline_memory_calls
            integrity_totals["treatment_memory_tool_calls"] += treatment_memory_calls
            comparisons[treatment][phase] = {
                "paired_tasks": len(pairs),
                "success": {
                    "before_successes": sum(before_success),
                    "after_successes": sum(after_success),
                    "before_rate": before_rate,
                    "after_rate": after_rate,
                    "absolute_change": after_rate - before_rate,
                    "improved_tasks": sum(
                        not before and after
                        for before, after in zip(before_success, after_success, strict=True)
                    ),
                    "regressed_tasks": sum(
                        before and not after
                        for before, after in zip(before_success, after_success, strict=True)
                    ),
                },
                "input_tokens": _numeric_change(
                    before_totals.input_tokens, after_totals.input_tokens
                ),
                "cache_hit_tokens": _numeric_change(
                    before_totals.cache_hit_tokens, after_totals.cache_hit_tokens
                ),
                "cache_miss_tokens": _numeric_change(
                    before_totals.cache_miss_tokens, after_totals.cache_miss_tokens
                ),
                "output_tokens": _numeric_change(
                    before_totals.output_tokens, after_totals.output_tokens
                ),
                "provider_requests": _numeric_change(before_totals.requests, after_totals.requests),
                "provider_attempts": _numeric_change(
                    sum(item.provider_attempts for item, _ in pairs),
                    sum(item.provider_attempts for _, item in pairs),
                ),
                "mean_ttft_seconds": _numeric_change(
                    _mean_optional([item.ttft_seconds for item in before_usage]),
                    _mean_optional([item.ttft_seconds for item in after_usage]),
                ),
                "total_run_latency_seconds": _numeric_change(
                    round(sum(item.record.latency_seconds for item, _ in pairs), 6),
                    round(sum(item.record.latency_seconds for _, item in pairs), 6),
                ),
                "memory_tool_calls": _numeric_change(baseline_memory_calls, treatment_memory_calls),
                "integrity": {
                    "identical_prompt_pairs": prompt_matches,
                    "identical_starting_state_pairs": state_matches,
                    "identical_runtime_pairs": runtime_matches,
                    "baseline_memory_tool_calls": baseline_memory_calls,
                },
            }
    return {
        "baseline_condition": baseline_name,
        "comparisons": comparisons,
        "integrity": integrity_totals,
    }


def _numeric_change(before: int | float | None, after: int | float | None) -> dict[str, Any]:
    if before is None or after is None:
        return {
            "before": before,
            "after": after,
            "absolute_change": None,
            "relative_change": None,
        }
    difference = after - before
    return {
        "before": before,
        "after": after,
        "absolute_change": difference,
        "relative_change": difference / before if before != 0 else None,
    }


def _before_after_display(
    metric: dict[str, Any],
    *,
    percent: bool = False,
    digits: int = 0,
) -> str:
    before = metric.get("before_rate") if percent else metric.get("before")
    after = metric.get("after_rate") if percent else metric.get("after")
    change = metric.get("absolute_change")
    if before is None or after is None or change is None:
        return "—"
    if percent:
        return f"{before * 100:.2f}% → {after * 100:.2f}% ({change * 100:+.2f} pp)"
    if digits:
        return f"{before:.{digits}f} → {after:.{digits}f} ({change:+.{digits}f})"
    return f"{before} → {after} ({change:+})"


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{float(value) * 100:.2f}%"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")


__all__ = [
    "ContextEfficiencyRunRecord",
    "ExecutionStatus",
    "StructuredFailure",
    "append_jsonl",
    "build_context_efficiency_summary",
    "render_context_efficiency_summary",
    "write_json",
]
