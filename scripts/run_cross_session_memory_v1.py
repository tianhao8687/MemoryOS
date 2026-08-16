from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from memoryos.evaluation.context_efficiency import ContextEfficiencyCondition
from memoryos.evaluation.context_efficiency_runner import load_context_efficiency_runtime
from memoryos.evaluation.context_efficiency_runtime import ConditionPolicy
from memoryos.evaluation.deepseek_harness_agent import (
    MEMORYOS_PLUGIN_VERSION,
    DeepSeekHarnessCodingAgent,
    DeepSeekHarnessRuntime,
)
from memoryos.evaluation.openai_compatible_coding_agent import AgentRunStatus, ToolEvent
from memoryos.evaluation.provider_usage import CachePhase, ProviderUsageRecord, aggregate_usage

_SESSION_ID = re.compile(r"^session-[0-9a-f-]{36}$", re.IGNORECASE)
_ABSTENTION = re.compile(
    r"不知道|无法(?:确认|得知|判断)|没有(?:之前|历史|相关).{0,12}(?:信息|上下文|依据|记录)|"
    r"缺少.{0,12}(?:历史|依据|上下文)|不能(?:确定|确认)|无从得知",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (Decimal, Path)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=encoded, headers=headers, method=method)  # noqa: S310
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            value = json.load(response)
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MemoryOS HTTP {exc.code}: {message[:2000]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("MemoryOS returned a non-object JSON response")
    return value


class MemoryOSProcess:
    """Own one real MemoryOS HTTP process and its persistent data directory."""

    def __init__(self, *, data_dir: Path, log_dir: Path, project_root: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.log_dir = log_dir.resolve()
        self.project_root = project_root.resolve()
        self.process: subprocess.Popen[str] | None = None
        self.base_url: str | None = None
        self.token: str | None = None
        self.start_count = 0
        self.history: list[dict[str, Any]] = []
        self._log_handle: Any = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("MemoryOS process is already running")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        port = _available_port()
        self.start_count += 1
        log_path = self.log_dir / f"service-start-{self.start_count:02d}.log"
        self._log_handle = log_path.open("w", encoding="utf-8", newline="\n")
        environment = dict(os.environ)
        environment.update(
            {
                "MEMORYOS_CONTEXT_COMPILER_MODE": "msc",
                "MEMORYOS_ANN_ENABLED": "false",
                "PYTHONPATH": str(self.project_root),
            }
        )
        command = [
            sys.executable,
            "-m",
            "memoryos",
            "--data-dir",
            str(self.data_dir),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-open",
        ]
        started_at = _now()
        self.process = subprocess.Popen(
            command,
            cwd=self.project_root,
            env=environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                health = _http_json(self.base_url + "/api/health", timeout=1)
                if health.get("ok") is True:
                    token_path = self.data_dir / "auth.token"
                    self.token = token_path.read_text(encoding="utf-8").strip()
                    self.history.append(
                        {
                            "event": "started",
                            "pid": self.process.pid,
                            "at": started_at,
                            "base_url": self.base_url,
                            "log": str(log_path),
                        }
                    )
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.1)
        return_code = self.process.poll()
        self.stop()
        raise RuntimeError(
            f"MemoryOS service failed to become ready (return_code={return_code}): {last_error}"
        )

    def stop(self) -> dict[str, Any]:
        if self.process is None:
            return {"was_running": False, "exit_confirmed": True}
        process = self.process
        pid = process.pid
        process.terminate()
        try:
            return_code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)
        stopped = {
            "event": "stopped",
            "pid": pid,
            "at": _now(),
            "return_code": return_code,
            "exit_confirmed": process.poll() is not None,
        }
        self.history.append(stopped)
        self.process = None
        self.base_url = None
        self.token = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return stopped

    def list_memories(self, scope: str, *, status: str | None = None) -> list[dict[str, Any]]:
        if self.base_url is None or self.token is None:
            raise RuntimeError("MemoryOS service is not running")
        query = {
            "q": "",
            "scope_type": "repository",
            "scope_key": scope,
            "limit": "500",
        }
        if status is not None:
            query["status"] = status
        else:
            # The HTTP API intentionally defaults to current/active records.  The
            # runner uses ``status=None`` when it needs the complete supersede
            # chain, so make that intent explicit instead of silently receiving
            # only the current head.
            query["include_history"] = "true"
        payload = _http_json(
            self.base_url + "/api/memories?" + urlencode(query),
            token=self.token,
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError("MemoryOS search response has no items list")
        memories: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("memory"), dict):
                raise RuntimeError("MemoryOS search returned an invalid memory item")
            memories.append(dict(item["memory"]))
        return sorted(memories, key=lambda item: str(item.get("id")))

    def endpoint(self) -> tuple[str, str]:
        if self.base_url is None or self.token is None:
            raise RuntimeError("MemoryOS service is not running")
        return self.base_url, self.token


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RemoteMemoryBackend:
    """Frozen Harness bridge backend that forwards to a real MemoryOS process."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        task: str,
        repository: str,
        budget_tokens: int,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.task = task
        self.repository = repository
        self.budget_tokens = budget_tokens
        self.policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.MSC_CONTEXT_ONLY)
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if name == "memory_context":
                raw = _http_json(
                    self.base_url + "/api/context",
                    method="POST",
                    body=arguments,
                    token=self.token,
                )
                result = {
                    "context": raw,
                    "experiment": {
                        "condition": self.policy.condition.value,
                        "detail_level": self.policy.detail_level.value,
                        "response_mode": "full",
                        "tool_profile": self.policy.tool_profile.value,
                    },
                }
            elif name == "memory_propose":
                raw = _http_json(
                    self.base_url + "/api/memories",
                    method="POST",
                    body=arguments,
                    token=self.token,
                )
                result = _required_object(raw, "memory")
            elif name == "memory_confirm":
                memory_id = str(arguments["memory_id"])
                body = {
                    key: value
                    for key, value in arguments.items()
                    if key in {"strategy", "rationale"}
                }
                raw = _http_json(
                    self.base_url + f"/api/memories/{memory_id}/confirm",
                    method="POST",
                    body=body,
                    token=self.token,
                )
                result = _required_object(raw, "memory")
            else:
                raise ValueError(f"unsupported remote MemoryOS tool: {name}")
            wrapper = {"ok": True, "result": result}
        except Exception as exc:
            wrapper = {
                "ok": False,
                "error": {"code": type(exc).__name__, "message": str(exc)[:2000]},
            }
        with self._lock:
            self.events.append(
                {
                    "tool": name,
                    "safe_arguments": {
                        key: arguments[key] for key in ("key", "strategy") if key in arguments
                    },
                    "arguments_sha256": _sha256_text(_canonical_json(arguments)),
                    "result_sha256": _sha256_text(_canonical_json(wrapper)),
                    "ok": wrapper["ok"],
                    "duration_seconds": round(time.perf_counter() - started, 6),
                    "result": wrapper.get("result"),
                    "error": wrapper.get("error"),
                }
            )
        return wrapper


def _required_object(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RuntimeError(f"MemoryOS response has no {key} object")
    return dict(item)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class TurnRun:
    turn: int
    prompt: str
    session_id: str | None
    status: str
    failure_reason: str | None
    output: str
    provider_attempts: int
    usage: tuple[ProviderUsageRecord, ...]
    tool_events: tuple[ToolEvent, ...]
    remote_events: list[dict[str, Any]] = field(default_factory=list)
    write_token_attempts: tuple[dict[str, Any], ...] = ()
    controlled_evictions: tuple[dict[str, Any], ...] = ()

    def report(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "prompt_sha256": _sha256_text(self.prompt),
            "session_id": self.session_id,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "output": self.output,
            "provider_attempts": self.provider_attempts,
            "usage": _usage_summary(self.usage, self.provider_attempts),
            "write_token_accounting": _source_write_token_summary([self]),
            "memory_tool_events": [event.model_dump(mode="json") for event in self.tool_events],
            "memory_service_events": self.remote_events,
            "controlled_context_evictions": list(self.controlled_evictions),
        }


def _usage_summary(
    records: tuple[ProviderUsageRecord, ...] | list[ProviderUsageRecord],
    provider_attempts: int,
) -> dict[str, Any]:
    values = list(records)
    totals = aggregate_usage(values)
    return {
        "provider_attempts": provider_attempts,
        "completed_responses": totals.requests,
        "provider_retries": max(0, provider_attempts - totals.requests),
        "input_tokens": totals.input_tokens,
        "cache_hit_tokens": totals.cache_hit_tokens,
        "cache_miss_tokens": totals.cache_miss_tokens,
        "output_tokens": totals.output_tokens,
        "reasoning_tokens": totals.reasoning_tokens,
        "cost_usd": str(totals.cost_usd) if totals.cost_usd is not None else None,
        "provider_latency_seconds": totals.latency_seconds,
        "complete_ledger": provider_attempts == totals.requests,
    }


def _read_write_token_attempts(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        accounting = value.get("memory_write_token_accounting")
        if not isinstance(accounting, dict):
            continue
        integer_fields = (
            "write_tool_schema_tokens",
            "memory_write_result_tokens",
            "memory_write_visible_tokens",
        )
        if any(
            isinstance(accounting.get(name), bool)
            or not isinstance(accounting.get(name), int)
            or accounting[name] < 0
            for name in integer_fields
        ):
            continue
        records.append(dict(accounting))
    return tuple(records)


def _read_jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected an object at {path}:{line_number}")
        records.append(value)
    return tuple(records)


def _source_write_token_summary(turns: list[TurnRun]) -> dict[str, Any]:
    attempts = [record for turn in turns for record in turn.write_token_attempts]
    provider_attempts = sum(turn.provider_attempts for turn in turns)
    usage = [record for turn in turns for record in turn.usage]
    totals = aggregate_usage(usage)
    schema_values = {int(item["write_tool_schema_tokens"]) for item in attempts}
    tokenizer_ids = {str(item.get("tokenizer_id")) for item in attempts}
    tokenizer_kinds = {str(item.get("tokenizer_kind")) for item in attempts}
    counter_versions = {str(item.get("counter_version")) for item in attempts}
    return {
        "write_tool_schema_tokens": (
            next(iter(schema_values)) if len(schema_values) == 1 else None
        ),
        "cumulative_write_tool_schema_tokens": sum(
            int(item["write_tool_schema_tokens"]) for item in attempts
        ),
        "memory_write_result_tokens": sum(
            int(item["memory_write_result_tokens"]) for item in attempts
        ),
        "memory_write_visible_tokens": sum(
            int(item["memory_write_visible_tokens"]) for item in attempts
        ),
        "provider_input_tokens": totals.input_tokens,
        "provider_attempts": provider_attempts,
        "accounted_provider_attempts": len(attempts),
        "completed_provider_responses": totals.requests,
        "complete_accounting": (
            len(attempts) == provider_attempts == totals.requests
            and len(schema_values) == 1
            and len(tokenizer_ids) == 1
            and tokenizer_kinds == {"estimated"}
            and len(counter_versions) == 1
        ),
        "write_component_token_source": "estimated",
        "provider_input_token_source": "provider_exact",
        "tokenizer_id": next(iter(tokenizer_ids)) if len(tokenizer_ids) == 1 else None,
        "counter_version": (next(iter(counter_versions)) if len(counter_versions) == 1 else None),
    }


def _aggregate_source_write_tokens(case_reports: list[dict[str, Any]]) -> dict[str, Any]:
    per_case = {
        str(item["case_id"]): dict(item["session_a"]["write_token_accounting"])
        for item in case_reports
    }
    schema_values = {
        int(value["write_tool_schema_tokens"])
        for value in per_case.values()
        if isinstance(value.get("write_tool_schema_tokens"), int)
    }
    totals = {
        "write_tool_schema_tokens": (
            next(iter(schema_values)) if len(schema_values) == 1 else None
        ),
        "cumulative_write_tool_schema_tokens": sum(
            int(value["cumulative_write_tool_schema_tokens"]) for value in per_case.values()
        ),
        "memory_write_result_tokens": sum(
            int(value["memory_write_result_tokens"]) for value in per_case.values()
        ),
        "memory_write_visible_tokens": sum(
            int(value["memory_write_visible_tokens"]) for value in per_case.values()
        ),
        "provider_input_tokens": sum(
            int(value["provider_input_tokens"]) for value in per_case.values()
        ),
        "provider_attempts": sum(int(value["provider_attempts"]) for value in per_case.values()),
        "complete_accounting": (
            len(per_case) == 3
            and len(schema_values) == 1
            and all(value["complete_accounting"] for value in per_case.values())
        ),
    }
    return {
        "definitions": {
            "write_tool_schema_tokens": (
                "One-copy unicode-heuristic-v1 estimate of the model-visible "
                "memory_propose and memory_confirm schemas in a Session A request."
            ),
            "memory_write_visible_tokens": (
                "Cumulative unicode-heuristic-v1 estimate across Session A provider "
                "requests: write schemas plus replayed write-tool results."
            ),
            "provider_input_tokens": (
                "Exact Session A input tokens summed from DeepSeek provider usage."
            ),
        },
        "per_case": per_case,
        "totals": totals,
    }


def _session_id_from_home(home: Path) -> str | None:
    values = {
        path.parent.name
        for path in home.rglob("session.jsonl.zstd")
        if _SESSION_ID.fullmatch(path.parent.name)
    }
    if len(values) != 1:
        return None
    return next(iter(values))


def _prepare_workspace(fixture: Path, filesystem_root: Path) -> Path:
    if not filesystem_root.is_dir() or not filesystem_root.is_mount():
        raise RuntimeError(f"session filesystem root is not a dedicated mount: {filesystem_root}")
    workspace = filesystem_root / "workspace"
    if workspace.exists():
        raise RuntimeError(f"refusing to reuse session workspace: {workspace}")
    shutil.copytree(fixture, workspace)
    commands = (
        ("git", "init", "--quiet"),
        ("git", "config", "user.email", "cross-session@example.invalid"),
        ("git", "config", "user.name", "Cross Session Fixture"),
        ("git", "add", "."),
        ("git", "commit", "--quiet", "-m", "fixture"),
    )
    for command in commands:
        subprocess.run(command, cwd=workspace, check=True, capture_output=True, text=True)
    return workspace


def _run_turn(
    *,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    filesystem_root: Path,
    workspace: Path,
    prompt: str,
    repository: str,
    run_id: str,
    task_id: str,
    logical_session: str,
    turn: int,
    backend: RemoteMemoryBackend | None,
    resume_session_id: str | None = None,
    write_profile: bool = False,
    evaluation_history_char_limit: int | None = None,
    evaluation_sentinel: str | None = None,
) -> TurnRun:
    state = filesystem_root / "state" / f"turn-{turn:02d}"
    home = filesystem_root / "home"
    namespace = _sha256_text(logical_session)
    result = DeepSeekHarnessCodingAgent(runtime, project_root=project_root).run(
        workspace=workspace,
        memory_tools=backend,
        state_dir=state,
        harness_home=home,
        filesystem_root=filesystem_root,
        task=prompt,
        repository=repository,
        run_id=run_id,
        task_id=task_id,
        condition=(
            ContextEfficiencyCondition.MSC_CONTEXT_ONLY.value
            if backend is not None
            else ContextEfficiencyCondition.NO_MEMORY.value
        ),
        cache_phase=CachePhase.COLD,
        cache_namespace=namespace,
        budget_tokens=1200,
        resume_session_id=resume_session_id,
        prompt_override=prompt,
        memory_tool_profile="cross-session-write" if write_profile else "read-only",
        evaluation_history_char_limit=evaluation_history_char_limit,
        evaluation_sentinel=evaluation_sentinel,
        enforce_budget=False,
    )
    write_token_attempts = _read_write_token_attempts(state / "provider-attempts.jsonl")
    controlled_evictions = _read_jsonl_objects(state / "controlled-context-evictions.jsonl")
    session_id = _session_id_from_home(home)
    return TurnRun(
        turn=turn,
        prompt=prompt,
        session_id=session_id,
        status=result.status.value,
        failure_reason=result.failure_reason,
        output=result.message or "",
        provider_attempts=result.provider_attempts,
        usage=result.usage,
        tool_events=result.tool_events,
        remote_events=list(backend.events) if backend is not None else [],
        write_token_attempts=write_token_attempts,
        controlled_evictions=controlled_evictions,
    )


def _run_source_case(
    *,
    case: dict[str, Any],
    cases_root: Path,
    campaign: Path,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    service: MemoryOSProcess,
    campaign_id: str,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    filesystem_root = campaign / "session-roots" / f"{case_id}__source"
    fixture = cases_root / "fixtures" / str(case["fixture"]["fixture_id"])
    workspace = _prepare_workspace(fixture, filesystem_root)
    session_id: str | None = None
    turns: list[TurnRun] = []
    for item in case["session_a"]["turns"]:
        prompt = str(item["user"])
        base_url, token = service.endpoint()
        backend = RemoteMemoryBackend(
            base_url=base_url,
            token=token,
            task=prompt,
            repository=str(case["repository_scope"]),
            budget_tokens=1200,
        )
        turn = _run_turn(
            runtime=runtime,
            project_root=project_root,
            filesystem_root=filesystem_root,
            workspace=workspace,
            prompt=prompt,
            repository=str(case["repository_scope"]),
            run_id=f"{campaign_id}-{case_id}-source-t{item['turn']}",
            task_id=f"{case_id}-source-t{item['turn']}",
            logical_session=f"{campaign_id}:{case_id}:source",
            turn=int(item["turn"]),
            backend=backend,
            resume_session_id=session_id,
            write_profile=True,
        )
        turns.append(turn)
        print(
            f"source_turn_complete case={case_id} turn={item['turn']} "
            f"status={turn.status} attempts={turn.provider_attempts} "
            f"memory_calls={len(turn.tool_events)}",
            flush=True,
        )
        if session_id is None:
            session_id = turn.session_id
        if turn.session_id != session_id or turn.status != AgentRunStatus.COMPLETED.value:
            break
    active = service.list_memories(str(case["repository_scope"]), status="active")
    all_memories = service.list_memories(str(case["repository_scope"]), status=None)
    return {
        "session_id": session_id,
        "turns": turns,
        "active_memories": active,
        "all_memories": all_memories,
    }


def _run_retrieval_arm(
    *,
    case: dict[str, Any],
    arm: Literal["A", "B", "C"],
    cases_root: Path,
    campaign: Path,
    runtime: DeepSeekHarnessRuntime,
    project_root: Path,
    service: MemoryOSProcess,
    campaign_id: str,
) -> TurnRun:
    case_id = str(case["case_id"])
    root = campaign / "session-roots" / f"{case_id}__{arm.lower()}"
    fixture = cases_root / "fixtures" / str(case["fixture"]["fixture_id"])
    workspace = _prepare_workspace(fixture, root)
    prompt = str(case["session_b"]["prompt"])
    repository = (
        str(case["wrong_repository_scope"]) if arm == "C" else str(case["repository_scope"])
    )
    backend: RemoteMemoryBackend | None = None
    if arm != "A":
        base_url, token = service.endpoint()
        backend = RemoteMemoryBackend(
            base_url=base_url,
            token=token,
            task=prompt,
            repository=repository,
            budget_tokens=1200,
        )
    run = _run_turn(
        runtime=runtime,
        project_root=project_root,
        filesystem_root=root,
        workspace=workspace,
        prompt=prompt,
        repository=repository,
        run_id=f"{campaign_id}-{case_id}-retrieval-{arm.lower()}",
        task_id=f"{case_id}-retrieval-{arm.lower()}",
        logical_session=f"{campaign_id}:{case_id}:retrieval:{arm}",
        turn=1,
        backend=backend,
    )
    print(
        f"retrieval_complete case={case_id} arm={arm} status={run.status} "
        f"attempts={run.provider_attempts} memory_calls={len(run.tool_events)}",
        flush=True,
    )
    return run


def _contains_all(text: str, terms: list[str]) -> bool:
    folded = text.casefold()
    return all(str(term).casefold() in folded for term in terms)


def _fact_checks(text: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "fact_id": fact["fact_id"],
            "assertion": fact["assertion"],
            "required_terms": fact["required_terms"],
            "present": _contains_all(text, list(fact["required_terms"])),
        }
        for fact in facts
    ]


def _memory_text(memories: list[dict[str, Any]]) -> str:
    return "\n".join(f"{item.get('title', '')}\n{item.get('content', '')}" for item in memories)


def _source_score(case: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    turns: list[TurnRun] = source["turns"]
    tools = [event.tool for turn in turns for event in turn.tool_events]
    active = source["active_memories"]
    text = _memory_text(active)
    required = _fact_checks(text, list(case["session_a"]["required_confirmed_facts"]))
    forbidden = []
    for truth in case["session_a"]["forbidden_confirmed_truths"]:
        matching_ids = [
            str(memory.get("id"))
            for memory in active
            if _contains_all(
                f"{memory.get('title', '')}\n{memory.get('content', '')}",
                list(truth["forbidden_terms"]),
            )
        ]
        forbidden.append(
            {
                "assertion": truth["assertion"],
                "forbidden_terms": truth["forbidden_terms"],
                "promoted": bool(matching_ids),
                "memory_ids": matching_ids,
            }
        )
    expected_turns = int(case["session_a"]["conversation_rounds"])
    session_ids = {turn.session_id for turn in turns if turn.session_id is not None}
    source_prompts = "\n".join(str(item["user"]) for item in case["session_a"]["turns"])
    forbidden_prompt_terms = case["_source_prompt_forbidden_terms"]
    prompt_hits = [
        term for term in forbidden_prompt_terms if str(term).casefold() in source_prompts.casefold()
    ]
    checks: dict[str, Any] = {
        "memory_prompt_terms_present": bool(prompt_hits),
        "memory_prompt_term_hits": prompt_hits,
        "memory_propose_called": "memory_propose" in tools,
        "memory_confirm_called": "memory_confirm" in tools,
        "memory_propose_calls": tools.count("memory_propose"),
        "memory_confirm_calls": tools.count("memory_confirm"),
        "confirmed_memory_count": len(active),
        "required_fact_checks": required,
        "forbidden_truth_checks": forbidden,
        "all_six_turns_completed": (
            len(turns) == expected_turns
            and all(turn.status == AgentRunStatus.COMPLETED.value for turn in turns)
        ),
        "one_logical_session": len(session_ids) == 1,
        "complete_provider_usage_ledger": all(
            turn.provider_attempts == len(turn.usage) for turn in turns
        ),
    }
    checks["passed"] = bool(
        not checks["memory_prompt_terms_present"]
        and checks["memory_propose_called"]
        and checks["memory_confirm_called"]
        and checks["confirmed_memory_count"] >= 1
        and all(item["present"] for item in required)
        and not any(item["promoted"] for item in forbidden)
        and checks["all_six_turns_completed"]
        and checks["one_logical_session"]
        and checks["complete_provider_usage_ledger"]
    )
    return checks


def _retrieval_score(
    case: dict[str, Any],
    arm: Literal["A", "B", "C"],
    run: TurnRun,
) -> dict[str, Any]:
    answer = run.output
    facts = list(case["session_a"]["required_confirmed_facts"])
    fact_checks = _fact_checks(answer, facts)
    tool_names = [event.tool for event in run.tool_events]
    context_calls = tool_names.count("memory_context")
    base: dict[str, Any] = {
        "session_id": run.session_id,
        "status": run.status,
        "failure_reason": run.failure_reason,
        "answer": answer,
        "answer_sha256": _sha256_text(answer),
        "memory_context_calls": context_calls,
        "fact_checks": fact_checks,
        "complete_target_answer": all(item["present"] for item in fact_checks),
        "usage": _usage_summary(run.usage, run.provider_attempts),
        "memory_tool_events": [event.model_dump(mode="json") for event in run.tool_events],
        "memory_service_events": run.remote_events,
    }
    completed = run.status == AgentRunStatus.COMPLETED.value
    if arm == "A":
        history_hits = [
            value
            for value in case["session_b"]["no_memory"]["history_only_facts"]
            if str(value).casefold() in answer.casefold()
        ]
        base.update(
            {
                "abstained": bool(_ABSTENTION.search(answer)),
                "history_only_fact_hits": history_hits,
                "leaked_target_answer": bool(base["complete_target_answer"]),
            }
        )
        base["passed"] = bool(
            completed
            and base["abstained"]
            and not history_hits
            and not base["complete_target_answer"]
            and context_calls == 0
            and base["usage"]["complete_ledger"]
        )
    elif arm == "B":
        base.update(
            {
                "memory_context_called": context_calls == 1,
                "recalled_required_facts": all(item["present"] for item in fact_checks),
                "answer_correct": all(item["present"] for item in fact_checks),
            }
        )
        base["passed"] = bool(
            completed
            and base["memory_context_called"]
            and base["answer_correct"]
            and base["usage"]["complete_ledger"]
        )
    else:
        canaries = list(case["session_b"]["wrong_scope"]["leakage_canaries"])
        context_payload = _canonical_json(run.remote_events)
        answer_hits = [item for item in canaries if str(item).casefold() in answer.casefold()]
        context_hits = [
            item for item in canaries if str(item).casefold() in context_payload.casefold()
        ]
        base.update(
            {
                "memory_context_called": context_calls == 1,
                "answer_leakage_canary_hits": answer_hits,
                "context_leakage_canary_hits": context_hits,
                "target_memory_returned": bool(context_hits),
                "target_answer_leaked": bool(answer_hits),
            }
        )
        base["passed"] = bool(
            completed
            and base["memory_context_called"]
            and not answer_hits
            and not context_hits
            and not base["complete_target_answer"]
            and base["usage"]["complete_ledger"]
        )
    return base


def _semantic_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "scope_type", "scope_key", "status", "title", "content", "category")
    return [{key: item.get(key) for key in keys} for item in memories]


def _preflight(
    cases_doc: dict[str, Any],
    runtime: DeepSeekHarnessRuntime,
    cases_root: Path,
    campaign: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    cases = cases_doc.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        errors.append("cases.json must contain exactly three cases")
        cases = []
    if runtime.provider_retry_limit != 0:
        errors.append("provider retry limit must be zero")
    if runtime.reasoning_effort != "high" or runtime.max_output_tokens != 8192:
        errors.append("runtime must use high reasoning and 8192 max output tokens")
    if not os.environ.get(runtime.api_key_environment):
        errors.append(f"{runtime.api_key_environment} is unavailable")
    forbidden_terms = list(cases_doc.get("source_prompt_forbidden_terms", []))
    fixture_digests: dict[str, str] = {}
    for case in cases:
        case_id = str(case["case_id"])
        prompts = "\n".join(str(item["user"]) for item in case["session_a"]["turns"])
        hits = [term for term in forbidden_terms if str(term).casefold() in prompts.casefold()]
        if hits:
            errors.append(f"{case_id} source prompt contains forbidden terms: {hits}")
        fixture = cases_root / "fixtures" / str(case["fixture"]["fixture_id"])
        files = sorted(path for path in fixture.rglob("*") if path.is_file())
        if not files:
            errors.append(f"{case_id} fixture is missing")
            continue
        fixture_text = "\n".join(path.read_text(encoding="utf-8") for path in files)
        canaries = case["session_b"]["wrong_scope"]["leakage_canaries"]
        leaks = [value for value in canaries if str(value).casefold() in fixture_text.casefold()]
        if leaks:
            errors.append(f"{case_id} fixture contains leakage canaries: {leaks}")
        digest_input = b"".join(
            path.relative_to(fixture).as_posix().encode("utf-8") + b"\0" + path.read_bytes()
            for path in files
        )
        fixture_digests[case_id] = _sha256_bytes(digest_input)
        for suffix in ("source", "a", "b", "c"):
            root = campaign / "session-roots" / f"{case_id}__{suffix}"
            if not root.is_dir() or not root.is_mount():
                errors.append(f"dedicated session mount is unavailable: {root}")
    return {
        "passed": not errors,
        "errors": errors,
        "provider_requests": 0,
        "fixture_sha256": fixture_digests,
    }


def _failure_source(exc: Exception) -> dict[str, Any]:
    return {
        "session_id": None,
        "turns": [],
        "active_memories": [],
        "all_memories": [],
        "runner_error": f"{type(exc).__name__}: {exc}",
    }


def _failure_retrieval(prompt: str, exc: Exception) -> TurnRun:
    return TurnRun(
        turn=1,
        prompt=prompt,
        session_id=None,
        status=AgentRunStatus.FAILED.value,
        failure_reason="runner_error",
        output=f"{type(exc).__name__}: {exc}",
        provider_attempts=0,
        usage=(),
        tool_events=(),
    )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# MemoryOS Cross-Session Memory E2E v1",
        "",
        f"Status: `{report['status']}`",
        "",
        "| Case | Source write | Restart | A abstain | B recall | C isolated | Pass |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case in report["cases"]:
        lines.append(
            "| {case_id} | {source} | {restart} | {a} | {b} | {c} | {passed} |".format(
                case_id=case["case_id"],
                source="yes" if case["session_a"]["passed"] else "no",
                restart="yes" if case["restart"]["passed"] else "no",
                a="yes" if case["no_memory"]["passed"] else "no",
                b="yes" if case["same_scope"]["passed"] else "no",
                c="yes" if case["wrong_scope"]["passed"] else "no",
                passed="yes" if case["passed"] else "no",
            )
        )
    totals = report["observability"]
    source_tokens = report["session_a_token_accounting"]
    source_totals = source_tokens["totals"]
    lines.extend(
        [
            "",
            f"Provider attempts: {totals['provider_attempts']}",
            f"Input tokens: {totals['input_tokens']}",
            f"Output tokens: {totals['output_tokens']}",
            f"Reasoning tokens: {totals['reasoning_tokens']}",
            f"Cost (USD): {totals['cost_usd']}",
            "",
            "Token and cost values are observability only and are not pass gates.",
            "",
            "## Session A write-token accounting",
            "",
            "| Case | Write schema (one copy, estimated) | "
            "Memory write visible (cumulative, estimated) | Provider input (exact) |",
            "|---|---:|---:|---:|",
            *(
                f"| {case_id} | {value['write_tool_schema_tokens']} | "
                f"{value['memory_write_visible_tokens']} | {value['provider_input_tokens']} |"
                for case_id, value in source_tokens["per_case"].items()
            ),
            f"| **three-case total** | **{source_totals['write_tool_schema_tokens']}** | "
            f"**{source_totals['memory_write_visible_tokens']}** | "
            f"**{source_totals['provider_input_tokens']}** |",
            "",
            "Write-component counts use unicode-heuristic-v1; provider input is provider-exact.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Cross-Session Memory E2E v1")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    arguments = parser.parse_args()

    cases_path = arguments.cases.resolve(strict=True)
    runtime_path = arguments.runtime.resolve(strict=True)
    project_root = arguments.project_root.resolve(strict=True)
    campaign = arguments.campaign_root.resolve(strict=True)
    report_path = campaign / "report.json"
    if report_path.exists():
        raise RuntimeError(f"refusing to reuse completed campaign: {campaign}")
    cases_doc = json.loads(cases_path.read_text(encoding="utf-8"))
    loaded_runtime = load_context_efficiency_runtime(runtime_path)
    if not isinstance(loaded_runtime, DeepSeekHarnessRuntime):
        raise TypeError("cross-session test requires a DeepSeek Harness runtime")
    preflight = _preflight(cases_doc, loaded_runtime, cases_path.parent, campaign)
    _write_json(campaign / "preflight.json", preflight)
    if not preflight["passed"]:
        raise RuntimeError("cross-session preflight failed: " + "; ".join(preflight["errors"]))

    campaign_id = "cross-session-v1-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    cases: list[dict[str, Any]] = list(cases_doc["cases"])
    for case in cases:
        case["_source_prompt_forbidden_terms"] = cases_doc["source_prompt_forbidden_terms"]
    services: dict[str, MemoryOSProcess] = {}
    source_results: dict[str, dict[str, Any]] = {}
    restart_results: dict[str, dict[str, Any]] = {}
    retrieval_results: dict[tuple[str, str], TurnRun] = {}
    started_at = _now()
    try:
        print("phase_start name=source parallel_sessions=3", flush=True)
        for case in cases:
            case_id = str(case["case_id"])
            service = MemoryOSProcess(
                data_dir=campaign / "memory-stores" / case_id,
                log_dir=campaign / "service-logs" / case_id,
                project_root=project_root,
            )
            service.start()
            services[case_id] = service

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="cross-session-source") as pool:
            source_futures = {
                pool.submit(
                    _run_source_case,
                    case=case,
                    cases_root=cases_path.parent,
                    campaign=campaign,
                    runtime=loaded_runtime,
                    project_root=project_root,
                    service=services[str(case["case_id"])],
                    campaign_id=campaign_id,
                ): case
                for case in cases
            }
            for source_future in as_completed(source_futures):
                case = source_futures[source_future]
                case_id = str(case["case_id"])
                try:
                    source_results[case_id] = source_future.result()
                except Exception as exc:
                    source_results[case_id] = _failure_source(exc)

        for case in cases:
            case_id = str(case["case_id"])
            service = services[case_id]
            before = service.list_memories(str(case["repository_scope"]), status="active")
            old_pid = service.process.pid if service.process is not None else None
            stop = service.stop()
            service.start()
            new_pid = service.process.pid if service.process is not None else None
            after = service.list_memories(str(case["repository_scope"]), status="active")
            restart_results[case_id] = {
                "agent_restarted": True,
                "memoryos_restarted": old_pid is not None and new_pid is not None,
                "old_memoryos_pid": old_pid,
                "new_memoryos_pid": new_pid,
                "old_process_exit_confirmed": stop["exit_confirmed"],
                "process_id_changed": old_pid != new_pid,
                "persistent_data_directory_preserved": True,
                "active_memories_before": _semantic_memories(before),
                "active_memories_after": _semantic_memories(after),
                "active_memory_semantics_preserved": (
                    _semantic_memories(before) == _semantic_memories(after)
                ),
                "source_harness_home_mounted_into_retrieval": False,
                "source_workspace_mounted_into_retrieval": False,
                "transcript_reused": False,
            }
            restart_results[case_id]["passed"] = bool(
                restart_results[case_id]["memoryos_restarted"]
                and restart_results[case_id]["old_process_exit_confirmed"]
                and restart_results[case_id]["process_id_changed"]
                and restart_results[case_id]["active_memory_semantics_preserved"]
            )

        print("phase_start name=retrieval parallel_sessions=9", flush=True)
        with ThreadPoolExecutor(
            max_workers=9, thread_name_prefix="cross-session-retrieval"
        ) as pool:
            retrieval_arms: tuple[Literal["A", "B", "C"], ...] = ("A", "B", "C")
            retrieval_futures = {
                pool.submit(
                    _run_retrieval_arm,
                    case=case,
                    arm=arm,
                    cases_root=cases_path.parent,
                    campaign=campaign,
                    runtime=loaded_runtime,
                    project_root=project_root,
                    service=services[str(case["case_id"])],
                    campaign_id=campaign_id,
                ): (case, arm)
                for case in cases
                for arm in retrieval_arms
            }
            for retrieval_future in as_completed(retrieval_futures):
                case, arm = retrieval_futures[retrieval_future]
                key = (str(case["case_id"]), arm)
                try:
                    retrieval_results[key] = retrieval_future.result()
                except Exception as exc:
                    retrieval_results[key] = _failure_retrieval(
                        str(case["session_b"]["prompt"]), exc
                    )
    finally:
        for service in services.values():
            service.stop()

    case_reports: list[dict[str, Any]] = []
    all_usage: list[ProviderUsageRecord] = []
    all_attempts = 0
    all_session_ids: list[str] = []
    for case in cases:
        case_id = str(case["case_id"])
        source = source_results.get(case_id, _failure_source(RuntimeError("source missing")))
        source_score = _source_score(case, source)
        source_turn_reports = [turn.report() for turn in source["turns"]]
        source_report = {
            **source_score,
            "session_id": source["session_id"],
            "turns": source_turn_reports,
            "write_token_accounting": _source_write_token_summary(source["turns"]),
            "active_memories": _semantic_memories(source["active_memories"]),
            "all_memories": _semantic_memories(source["all_memories"]),
            **({"runner_error": source["runner_error"]} if "runner_error" in source else {}),
        }
        if source["session_id"] is not None:
            all_session_ids.append(source["session_id"])
        for turn in source["turns"]:
            all_usage.extend(turn.usage)
            all_attempts += turn.provider_attempts

        arm_scores: dict[str, dict[str, Any]] = {}
        scored_arms: tuple[tuple[Literal["A", "B", "C"], str], ...] = (
            ("A", "no_memory"),
            ("B", "same_scope"),
            ("C", "wrong_scope"),
        )
        for arm, label in scored_arms:
            run = retrieval_results.get(
                (case_id, arm),
                _failure_retrieval(
                    str(case["session_b"]["prompt"]), RuntimeError("retrieval missing")
                ),
            )
            arm_scores[label] = _retrieval_score(case, arm, run)
            if run.session_id is not None:
                all_session_ids.append(run.session_id)
            all_usage.extend(run.usage)
            all_attempts += run.provider_attempts

        logical_ids = [
            source_report["session_id"],
            arm_scores["no_memory"]["session_id"],
            arm_scores["same_scope"]["session_id"],
            arm_scores["wrong_scope"]["session_id"],
        ]
        ids_fresh = None not in logical_ids and len(set(logical_ids)) == 4
        case_passed = bool(
            source_report["passed"]
            and restart_results.get(case_id, {}).get("passed") is True
            and arm_scores["no_memory"]["passed"]
            and arm_scores["same_scope"]["passed"]
            and arm_scores["wrong_scope"]["passed"]
            and ids_fresh
        )
        case_reports.append(
            {
                "case_id": case_id,
                "memory_class": case["memory_class"],
                "session_a": source_report,
                "restart": restart_results.get(
                    case_id, {"passed": False, "error": "restart missing"}
                ),
                **arm_scores,
                "fresh_session_ids": ids_fresh,
                "passed": case_passed,
            }
        )

    totals = _usage_summary(all_usage, all_attempts)
    total_sessions_unique = len(all_session_ids) == len(set(all_session_ids)) == 12
    passed_cases = sum(1 for item in case_reports if item["passed"])
    session_a_token_accounting = _aggregate_source_write_tokens(case_reports)
    report = {
        "schema_version": "1.0",
        "benchmark_id": cases_doc["benchmark_id"],
        "campaign_id": campaign_id,
        "started_at": started_at,
        "finished_at": _now(),
        "status": "passed" if passed_cases == 3 and total_sessions_unique else "failed",
        "controls": {
            "cases_sha256": _sha256_bytes(cases_path.read_bytes()),
            "runtime_sha256": _sha256_bytes(runtime_path.read_bytes()),
            "model": loaded_runtime.model,
            "memoryos_plugin_version": MEMORYOS_PLUGIN_VERSION,
            "reasoning_effort": loaded_runtime.reasoning_effort,
            "max_output_tokens": loaded_runtime.max_output_tokens,
            "provider_retry_limit": loaded_runtime.provider_retry_limit,
            "automatic_repetitions": 0,
            "parallel_source_sessions": 3,
            "parallel_retrieval_sessions": 9,
            "source_tool_profile": "cross-session-write",
            "retrieval_tool_profile": "read-only",
            "source_prompts_from_frozen_cases_only": True,
            "retrieval_prompts_identical_across_arms": True,
            "exact_prompt_override_used": True,
        },
        "preflight": preflight,
        "cases": case_reports,
        "gate": {
            "passed_cases": passed_cases,
            "required_cases": 3,
            "all_12_session_ids_unique": total_sessions_unique,
            "wrong_scope_leakage_canaries": sum(
                len(item["wrong_scope"]["answer_leakage_canary_hits"])
                + len(item["wrong_scope"]["context_leakage_canary_hits"])
                for item in case_reports
            ),
            "passed": passed_cases == 3 and total_sessions_unique,
        },
        "session_a_token_accounting": session_a_token_accounting,
        "observability": totals,
        "memoryos_process_history": {
            case_id: service.history for case_id, service in services.items()
        },
    }
    _write_json(report_path, report)
    (campaign / "report.md").write_text(_render_markdown(report), encoding="utf-8", newline="\n")
    print(
        f"cross_session_complete status={report['status']} "
        f"passed_cases={passed_cases}/3 attempts={all_attempts} "
        f"input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']}",
        flush=True,
    )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
