from __future__ import annotations

import asyncio
import copy
import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select

from memoryos.config import settings_for
from memoryos.context.token_meter import UnicodeHeuristicTokenCounter, canonical_json
from memoryos.db import Database
from memoryos.db.models import ContextSnapshotRow, MemoryRow
from memoryos.domain.schemas import ContextRequest, DetailLevel
from memoryos.engine import MemoryService
from memoryos.evaluation.context_efficiency import ContextEfficiencyCondition
from memoryos.evaluation.openai_compatible_coding_agent import ToolDefinition
from memoryos.evaluation.real_workload_memory import seed_memoryos
from memoryos.evaluation.real_workload_models import MemorySeedSpec
from memoryos.mcp_server.server import create_mcp_server


class CompilerMode(StrEnum):
    LEGACY = "legacy"
    MSC = "msc"


class ContextToolProfile(StrEnum):
    """Tool exposure for an experiment arm, including the true no-memory baseline."""

    NONE = "none"
    ALL = "all"
    CORE = "core"
    CONTEXT = "context"
    GOVERNANCE = "governance"
    DEBUG = "debug"


_POLICIES: dict[ContextEfficiencyCondition, dict[str, Any]] = {
    ContextEfficiencyCondition.LEGACY_FULL: {
        "compiler_mode": CompilerMode.LEGACY,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "full",
        "tool_profile": ContextToolProfile.ALL,
        "allow_explain": False,
        "use_previous_context": False,
        "memory_enabled": True,
    },
    ContextEfficiencyCondition.MSC_FULL: {
        "compiler_mode": CompilerMode.MSC,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "full",
        "tool_profile": ContextToolProfile.ALL,
        "allow_explain": False,
        "use_previous_context": False,
        "memory_enabled": True,
    },
    ContextEfficiencyCondition.MSC_PROGRESSIVE: {
        "compiler_mode": CompilerMode.MSC,
        "detail_level": DetailLevel.INDEX,
        "initial_response_mode": "full",
        "subsequent_response_mode": "full",
        "tool_profile": ContextToolProfile.ALL,
        "allow_explain": True,
        "use_previous_context": False,
        "memory_enabled": True,
    },
    ContextEfficiencyCondition.MSC_DELTA: {
        "compiler_mode": CompilerMode.MSC,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "delta",
        "tool_profile": ContextToolProfile.ALL,
        "allow_explain": True,
        "use_previous_context": True,
        "memory_enabled": True,
    },
    ContextEfficiencyCondition.MSC_DELTA_CORE: {
        "compiler_mode": CompilerMode.MSC,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "delta",
        "tool_profile": ContextToolProfile.CORE,
        "allow_explain": True,
        "use_previous_context": True,
        "memory_enabled": True,
    },
    ContextEfficiencyCondition.NO_MEMORY: {
        # Compiler fields remain frozen placeholders but are never instantiated.
        "compiler_mode": CompilerMode.LEGACY,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "full",
        "tool_profile": ContextToolProfile.NONE,
        "allow_explain": False,
        "use_previous_context": False,
        "memory_enabled": False,
    },
    ContextEfficiencyCondition.MSC_CONTEXT_ONLY: {
        # Same compiler and payload policy as MSC_FULL; only the agent-facing
        # MemoryOS schema is reduced to the one read operation this arm uses.
        "compiler_mode": CompilerMode.MSC,
        "detail_level": DetailLevel.FACT,
        "initial_response_mode": "full",
        "subsequent_response_mode": "full",
        "tool_profile": ContextToolProfile.CONTEXT,
        "allow_explain": False,
        "use_previous_context": False,
        "memory_enabled": True,
    },
}


class ConditionPolicy(BaseModel):
    """Frozen and hashable controller policy; the model cannot select its study arm."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    condition: ContextEfficiencyCondition
    compiler_mode: CompilerMode
    detail_level: DetailLevel
    initial_response_mode: Literal["full"]
    subsequent_response_mode: Literal["full", "delta"]
    tool_profile: ContextToolProfile
    allow_explain: bool
    use_previous_context: bool
    memory_enabled: bool

    @model_validator(mode="after")
    def require_registered_policy(self) -> ConditionPolicy:
        expected = _POLICIES[self.condition]
        actual = self.model_dump(exclude={"schema_version", "condition"})
        normalized = {
            key: value.value if isinstance(value, StrEnum) else value
            for key, value in expected.items()
        }
        if actual != normalized:
            raise ValueError("condition behavior is frozen and cannot be customized")
        return self

    @classmethod
    def for_condition(cls, condition: ContextEfficiencyCondition | str) -> ConditionPolicy:
        selected = ContextEfficiencyCondition(str(condition))
        return cls(condition=selected, **_POLICIES[selected])

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


def all_condition_policies() -> tuple[ConditionPolicy, ...]:
    return tuple(
        ConditionPolicy.for_condition(condition) for condition in ContextEfficiencyCondition
    )


class MemoryOSToolBackend:
    """Read-only in-process MemoryOS tools governed by one ConditionPolicy."""

    def __init__(
        self,
        *,
        data_dir: Path,
        policy: ConditionPolicy,
        task: str,
        repository: str,
        seeds: list[MemorySeedSpec],
        seed_database: bool,
        budget_tokens: int = 6000,
        tokenizer_id: str | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        if not policy.memory_enabled or policy.tool_profile is ContextToolProfile.NONE:
            raise ValueError("the no-memory condition cannot instantiate MemoryOS")
        if seed_database:
            if self.data_dir.exists() and any(self.data_dir.iterdir()):
                raise ValueError("refusing to seed a non-empty MemoryOS run directory")
            seed_memoryos(self.data_dir, seeds)
        settings = settings_for(
            self.data_dir,
            context_compiler_mode=policy.compiler_mode.value,
            mcp_tool_profile=policy.tool_profile.value,
        )
        self.database = Database(settings)
        self.database.initialize()
        self.service = MemoryService(self.database, settings)
        self.policy = policy
        self.task = task
        self.repository = repository
        self.budget_tokens = budget_tokens
        self.tokenizer_id = tokenizer_id
        self.context_calls = 0
        self.explain_calls = 0
        self.delta_hits = 0
        self.full_fallbacks = 0
        self.context_rebases = 0
        self.snapshot_misses = 0
        self.blocked_actions = 0
        self._previous_internal_context_id: str | None = None
        self._stable_by_internal: dict[str, str] = {}
        self._counter = UnicodeHeuristicTokenCounter()
        self._memory_payload_tokens = 0
        self._memory_wrapper_tokens = 0
        self._context_usage_totals = {
            "context_text_tokens": 0,
            "delivered_payload_tokens": 0,
            "payload_overhead_tokens": 0,
            "evidence_expansion_tokens": 0,
            "history_expansion_tokens": 0,
            "delta_tokens": 0,
            "full_context_tokens": 0,
        }
        self._definitions = self._load_definitions(settings)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def close(self) -> None:
        self.database.close()

    def accounting_snapshot(self) -> dict[str, int | None]:
        return {
            "memory_payload_tokens": self._memory_payload_tokens,
            "memory_wrapper_tokens": self._memory_wrapper_tokens,
            "context_rebases": self.context_rebases,
            "delta_hits": self.delta_hits,
            "full_fallbacks": self.full_fallbacks,
            "snapshot_misses": self.snapshot_misses,
            "memory_blocked_actions": self.blocked_actions,
            **self._context_usage_totals,
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "memory_context":
            return self._context()
        if name == "memory_explain":
            return self._explain(arguments)
        self.blocked_actions += 1
        return {
            "ok": False,
            "error": {
                "code": "benchmark_memory_read_only",
                "message": f"{name} is not enabled by the read-only experiment controller",
            },
        }

    def _context(self) -> dict[str, Any]:
        initial = self.context_calls == 0
        response_mode = (
            self.policy.initial_response_mode if initial else self.policy.subsequent_response_mode
        )
        previous = (
            self._previous_internal_context_id
            if self.policy.use_previous_context and not initial
            else None
        )
        raw = self.service.context(
            ContextRequest(
                task=self.task,
                repository=self.repository,
                budget_tokens=self.budget_tokens,
                tokenizer_id=self.tokenizer_id,
                detail_level=self.policy.detail_level,
                previous_context_id=previous,
                response_mode=response_mode,
            )
        )
        self.context_calls += 1
        internal_id = raw.get("context_id")
        if isinstance(internal_id, str):
            stable = self._stable_context_id(internal_id, raw)
            self._stable_by_internal[internal_id] = stable
            self._previous_internal_context_id = internal_id
        mode = raw.get("mode")
        fallback = raw.get("fallback_reason")
        if mode == "delta":
            self.delta_hits += 1
        elif not initial and self.policy.use_previous_context:
            self.full_fallbacks += 1
            if fallback in {
                "scope_mismatch",
                "tokenizer_mismatch",
                "policy_mismatch",
                "snapshot_expired",
                "snapshot_integrity_failure",
                "snapshot_unavailable",
            }:
                self.context_rebases += 1
            if fallback in {"snapshot_expired", "snapshot_unavailable"}:
                self.snapshot_misses += 1
        visible = self._model_visible_context(raw)
        payload_tokens = _context_payload_tokens(raw, self._counter)
        usage = raw.get("usage")
        if isinstance(usage, dict):
            for key in self._context_usage_totals:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._context_usage_totals[key] += value
        wrapper = {
            "context": visible,
            "experiment": {
                "condition": self.policy.condition.value,
                "detail_level": self.policy.detail_level.value,
                "response_mode": response_mode,
                "tool_profile": self.policy.tool_profile.value,
            },
        }
        self._memory_payload_tokens += payload_tokens
        self._memory_wrapper_tokens += max(
            0,
            self._counter.count_json(wrapper) - self._counter.count_json(visible),
        )
        return {"ok": True, "result": wrapper}

    def _explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_explain:
            self.blocked_actions += 1
            return {
                "ok": False,
                "error": {
                    "code": "explain_disabled_by_condition",
                    "message": "memory_explain is disabled in this frozen condition",
                },
            }
        memory_id = arguments.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            return {
                "ok": False,
                "error": {"code": "invalid_memory_id", "message": "memory_id is required"},
            }
        expected = arguments.get("expected_atom_sha256")
        if expected is not None and not isinstance(expected, str):
            return {
                "ok": False,
                "error": {"code": "invalid_atom_hash", "message": "atom hash must be a string"},
            }
        sections = arguments.get("sections")
        budget = arguments.get("budget_tokens")
        try:
            value = self.service.explain(
                memory_id,
                expected_atom_sha256=expected,
                sections=sections if isinstance(sections, list) else None,
                budget_tokens=(
                    budget if isinstance(budget, int) and not isinstance(budget, bool) else None
                ),
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": {"code": type(exc).__name__, "message": str(exc)[:2000]},
            }
        self.explain_calls += 1
        self._memory_payload_tokens += self._counter.count_json(value)
        return {"ok": True, "result": _strip_volatile(value)}

    def _stable_context_id(self, internal_id: str, raw: dict[str, Any]) -> str:
        with self.database.session() as session:
            row = session.get(ContextSnapshotRow, internal_id)
            if row is not None:
                payload = {
                    "items": row.items_json,
                    "policy_hash": row.policy_hash,
                    "scope_fingerprint": row.scope_fingerprint,
                    "tokenizer_id": row.tokenizer_id,
                }
                return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        semantic = _strip_volatile(raw)
        semantic.pop("context_id", None)
        semantic.pop("requires_base_context_id", None)
        return hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()

    def _model_visible_context(self, raw: dict[str, Any]) -> dict[str, Any]:
        stripped = _strip_volatile(raw)
        if not isinstance(stripped, dict):  # pragma: no cover - raw is statically a dict
            raise TypeError("context payload must remain an object")
        visible: dict[str, Any] = stripped
        # Accounting is retained out-of-band on ProviderUsageRecord and the run
        # record. It is not task evidence, and raw token fixed points can vary by
        # a token when internal run handles or timing values change width.
        visible.pop("usage", None)
        internal = raw.get("context_id")
        if isinstance(internal, str):
            visible["context_id"] = self._stable_by_internal[internal]
        base = raw.get("requires_base_context_id")
        if isinstance(base, str):
            visible["requires_base_context_id"] = self._stable_by_internal.get(
                base,
                hashlib.sha256(base.encode("utf-8")).hexdigest(),
            )
        if "context_id" not in visible:
            visible["context_fingerprint"] = hashlib.sha256(
                canonical_json(visible).encode("utf-8")
            ).hexdigest()
        return visible

    @staticmethod
    def _load_definitions(settings: Any) -> tuple[ToolDefinition, ...]:
        server = create_mcp_server(settings, settings.mcp_tool_profile)
        listed = asyncio.run(server.list_tools())
        return tuple(
            ToolDefinition(
                name=str(tool.name),
                description=str(tool.description or "MemoryOS tool"),
                parameters=copy.deepcopy(tool.inputSchema),
                category="memory",
            )
            for tool in listed
        )

    def selected_memory_ids(self) -> tuple[str, ...]:
        with self.database.session() as session:
            return tuple(sorted(str(value) for value in session.scalars(select(MemoryRow.id))))


def _strip_volatile(value: Any) -> Any:
    volatile = {
        "retrieval_run_id",
        "selection_latency_ms",
        "render_latency_ms",
        "duration_ms",
        "stage_timings_ms",
    }
    if isinstance(value, dict):
        return {
            str(key): _strip_volatile(item)
            for key, item in value.items()
            if str(key) not in volatile
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _context_payload_tokens(raw: dict[str, Any], counter: UnicodeHeuristicTokenCounter) -> int:
    usage = raw.get("usage")
    if isinstance(usage, dict):
        value = usage.get("delivered_payload_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return counter.count_json(raw)


__all__ = [
    "CompilerMode",
    "ConditionPolicy",
    "ContextEfficiencyCondition",
    "ContextToolProfile",
    "MemoryOSToolBackend",
    "all_condition_policies",
]
