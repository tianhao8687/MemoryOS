from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memoryos.context.token_meter import canonical_json
from memoryos.evaluation.openai_compatible_coding_agent import (
    AgentRunStatus,
    CodingAgentResult,
    ToolEvent,
)
from memoryos.evaluation.provider_usage import CachePhase, PricingSnapshot, ProviderUsageRecord

HARNESS_VERSION = "0.1.0-rc.5"
HARNESS_COMMIT = "47f943859bef60e4160492346772ded9b24f765a"
MEMORYOS_PLUGIN_VERSION = "0.1.18"

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$", re.IGNORECASE)
_MEMORY_DISPLAY_HANDLE = re.compile(
    r"^(?P<memory_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\s*@\s*(?P<atom_sha256>[0-9a-f]{64})$",
    re.IGNORECASE,
)
_HARNESS_SESSION_ID = re.compile(
    r"^session-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_HTTP_BODY = 1024 * 1024
_MAX_LOG_CHARS = 64_000
_TIMEOUT_RETURN_CODE = 124
_BUDGET_RETURN_CODE = 125
_USAGE_GUARD_STOP_MARKER = "MEMORYOS_USAGE_GUARD_STOP"
_TERMINATION_GRACE_SECONDS = 2
_BUDGET_POLL_SECONDS = 0.25
_STANDARD_PRESET_SOURCE = Path(
    "/opt/deepseek-harness/apps/cli/config/agent-presets/standard/agent.cordis.yml"
)
_STANDARD_PRESET_SOURCE_SHA256 = "cb98756a9ed76ca351a45a0ba138a97bf0ab7eead4fe2f1e9d1c9f9ec97937f0"
_DELEGATION_SECTION = "# ── delegation and workflows "
_REMAINING_TOOLS_SECTION = "# ── remaining model-facing rows "
_WEB_TOOL_SECTION = "# The `web` service and its search provider stay in the host composition"
_BACKGROUND_JOBS_SECTION = "# ── background jobs "
_COMPACTION_SECTION = "# ── compaction "
_HARNESS_ORIGINAL_PERSONA = (
    "You are a coding agent powered by the {{model}} model. Your working directory is {{cwd}}."
)
_HARNESS_STABLE_PERSONA = (
    "You are a coding agent powered by the {{model}} model. "
    "Work only in the assigned isolated workspace."
)
_DEEPSEEK_COMPACT_MEMORY_BUDGET_TOKENS = 512
_DEEPSEEK_OPTIMIZED_PRESET_V1 = "deepseek-optimized-offline"
_DEEPSEEK_OPTIMIZED_PRESET_V2 = "deepseek-optimized-offline-v2"
_DEEPSEEK_OPTIMIZED_PRESET = "deepseek-optimized-offline-v3"
_DEEPSEEK_COMPACT_PRESETS = {
    _DEEPSEEK_OPTIMIZED_PRESET_V1,
    _DEEPSEEK_OPTIMIZED_PRESET_V2,
    _DEEPSEEK_OPTIMIZED_PRESET,
}
_DEEPSEEK_BUDGETED_PRESETS = {
    "standard-offline",
    _DEEPSEEK_OPTIMIZED_PRESET_V2,
    _DEEPSEEK_OPTIMIZED_PRESET,
}
_HARNESS_REQUEST_CONTROL_PATCH = (
    "# Keep auxiliary presentation work out of provider-request accounting.\n"
    "# Keep the global headless persona independent of the disposable workspace path.\n"
    "- id: system-prompt\n"
    "  config:\n"
    "    persona: >-\n"
    f"      {_HARNESS_STABLE_PERSONA}\n"
    "\n"
    "- id: session-title-llm\n"
    "  disabled: true\n"
)
_DEEPSEEK_NO_PATCH_MAX_REQUESTS = 20
_DEEPSEEK_NO_PATCH_MAX_INPUT_TOKENS = 800_000
_DEEPSEEK_NO_PATCH_MAX_OUTPUT_TOKENS = 100_000_000
_DEEPSEEK_SOFT_MAX_REQUESTS = 30
_DEEPSEEK_SOFT_MAX_INPUT_TOKENS = 1_500_000
_DEEPSEEK_SOFT_MAX_OUTPUT_TOKENS = 100_000_000
_DEEPSEEK_PROGRESS_GRACE_REQUESTS = 6
_DEEPSEEK_HARD_MAX_REQUESTS = 60
_DEEPSEEK_HARD_MAX_INPUT_TOKENS = 3_000_000
_DEEPSEEK_HARD_MAX_OUTPUT_TOKENS = 100_000_000


class DeepSeekHarnessRuntime(BaseModel):
    """Frozen inputs for the pinned DeepSeek Harness headless adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    adapter: Literal["deepseek_harness"] = "deepseek_harness"
    provider: Literal["deepseek"] = "deepseek"
    harness_provider: Literal["deepseek-official"] = "deepseek-official"
    executable: str = Field(default="dsh", min_length=1, max_length=1000)
    profile: Literal["headless"] = "headless"
    agent_preset: Literal[
        "minimal",
        "standard",
        "code",
        "standard-offline",
        "deepseek-optimized-offline",
        "deepseek-optimized-offline-v2",
        "deepseek-optimized-offline-v3",
    ] = "minimal"
    reasoning_effort: Literal["off", "high", "max"] = "max"
    max_output_tokens: int = Field(default=256_000, ge=1, le=256_000)
    permission_mode: Literal["danger-full-access"] = "danger-full-access"
    effective_permission_mode: Literal["dedicated-condition-mount-write"] = (
        "dedicated-condition-mount-write"
    )
    sandbox_authority: Literal["outer-landlock-plus-shell-seccomp"] = (
        "outer-landlock-plus-shell-seccomp"
    )
    tool_shell_network: Literal["none"] = "none"
    tool_shell_read_scope: Literal["dedicated-condition-mount-system-only"] = (
        "dedicated-condition-mount-system-only"
    )
    tool_shell_launcher: Literal["/opt/memoryos/bin/bash"] = "/opt/memoryos/bin/bash"
    agent_read_scope: Literal["dedicated-condition-mount-runtime-only"] = (
        "dedicated-condition-mount-runtime-only"
    )
    agent_filesystem_root_policy: Literal["must-be-dedicated-mountpoint"] = (
        "must-be-dedicated-mountpoint"
    )
    agent_filesystem_launcher: Literal[
        "/opt/deepseek-harness/native/landlock-run/packages/linux-x64/bin/landlock-run"
    ] = "/opt/deepseek-harness/native/landlock-run/packages/linux-x64/bin/landlock-run"
    pnpm_version: Literal["11.7.0"] = "11.7.0"
    plugin_path: str = Field(
        default="integrations/deepseek-harness-memoryos", min_length=1, max_length=2000
    )
    harness_version: Literal["0.1.0-rc.5"] = "0.1.0-rc.5"
    harness_commit: Literal["47f943859bef60e4160492346772ded9b24f765a"] = (
        "47f943859bef60e4160492346772ded9b24f765a"
    )
    execution_environment: Literal["linux-docker-wsl2"] = "linux-docker-wsl2"
    execution_image: str = Field(min_length=80, max_length=500)
    api_key_environment: str = "DEEPSEEK_API_KEY"
    base_url: str = "https://api.deepseek.com"
    model: str = Field(min_length=1, max_length=300)
    model_revision: str = Field(min_length=1, max_length=300)
    quantization: Literal["provider-hosted"] = "provider-hosted"
    context_length: int = Field(ge=2048, le=2_000_000)
    install_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    run_timeout_seconds: int = Field(default=1800, ge=1, le=7200)
    memory_timeout_seconds: int = Field(default=30, ge=1, le=300)
    provider_retry_limit: int = Field(default=1, ge=0, le=5)
    no_patch_request_limit: int = Field(default=_DEEPSEEK_NO_PATCH_MAX_REQUESTS, ge=1, le=10_000)
    no_patch_input_token_limit: int = Field(
        default=_DEEPSEEK_NO_PATCH_MAX_INPUT_TOKENS, ge=1, le=100_000_000
    )
    no_patch_output_token_limit: int = Field(
        default=_DEEPSEEK_NO_PATCH_MAX_OUTPUT_TOKENS, ge=1, le=100_000_000
    )
    patch_preserving_request_limit: int = Field(
        default=_DEEPSEEK_SOFT_MAX_REQUESTS, ge=1, le=10_000
    )
    patch_preserving_input_token_limit: int = Field(
        default=_DEEPSEEK_SOFT_MAX_INPUT_TOKENS, ge=1, le=100_000_000
    )
    patch_preserving_output_token_limit: int = Field(
        default=_DEEPSEEK_SOFT_MAX_OUTPUT_TOKENS, ge=1, le=100_000_000
    )
    hard_request_limit: int = Field(default=_DEEPSEEK_HARD_MAX_REQUESTS, ge=1, le=10_000)
    hard_input_token_limit: int = Field(
        default=_DEEPSEEK_HARD_MAX_INPUT_TOKENS, ge=1, le=100_000_000
    )
    hard_output_token_limit: int = Field(
        default=_DEEPSEEK_HARD_MAX_OUTPUT_TOKENS, ge=1, le=100_000_000
    )
    progress_grace_requests: int = Field(default=_DEEPSEEK_PROGRESS_GRACE_REQUESTS, ge=0, le=100)
    pricing: PricingSnapshot

    def effective_memory_budget_tokens(self, requested: int) -> int:
        """Return the model-facing context budget frozen by this Harness preset."""

        if self.agent_preset in _DEEPSEEK_COMPACT_PRESETS:
            return min(requested, _DEEPSEEK_COMPACT_MEMORY_BUDGET_TOKENS)
        return requested

    @field_validator("api_key_environment")
    @classmethod
    def require_environment_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("api_key_environment must be a portable environment name")
        return value

    @field_validator("executable", "plugin_path")
    @classmethod
    def reject_unsafe_path_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("runtime paths cannot contain NUL")
        return value

    @field_validator("execution_image")
    @classmethod
    def require_digest_pinned_execution_image(cls, value: str) -> str:
        if not _IMAGE_NAME.fullmatch(value):
            raise ValueError("execution_image must be pinned by a sha256 digest")
        return value

    @field_validator("base_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url cannot contain credentials")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_frozen_price_row(self) -> DeepSeekHarnessRuntime:
        if self.pricing.find(self.provider, self.model) is None:
            raise ValueError("pricing snapshot must contain the exact DeepSeek model row")
        if self.patch_preserving_request_limit > self.hard_request_limit:
            raise ValueError("patch_preserving_request_limit cannot exceed hard_request_limit")
        if self.no_patch_request_limit > self.patch_preserving_request_limit:
            raise ValueError("no_patch_request_limit cannot exceed patch_preserving_request_limit")
        if self.patch_preserving_input_token_limit > self.hard_input_token_limit:
            raise ValueError(
                "patch_preserving_input_token_limit cannot exceed hard_input_token_limit"
            )
        if self.no_patch_input_token_limit > self.patch_preserving_input_token_limit:
            raise ValueError(
                "no_patch_input_token_limit cannot exceed patch_preserving_input_token_limit"
            )
        if self.patch_preserving_output_token_limit > self.hard_output_token_limit:
            raise ValueError(
                "patch_preserving_output_token_limit cannot exceed hard_output_token_limit"
            )
        if self.no_patch_output_token_limit > self.patch_preserving_output_token_limit:
            raise ValueError(
                "no_patch_output_token_limit cannot exceed patch_preserving_output_token_limit"
            )
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


class HarnessMemoryBackend(Protocol):
    policy: Any
    task: str
    repository: str
    budget_tokens: int

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bridge: MemoryOSHTTPBridge) -> None:
        self.bridge = bridge
        super().__init__(("127.0.0.1", 0), _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    server: _BridgeServer

    def do_POST(self) -> None:
        self.server.bridge.handle(self, "POST")

    def do_GET(self) -> None:
        self.server.bridge.handle(self, "GET")

    def log_message(self, format: str, *args: Any) -> None:
        return


class MemoryOSHTTPBridge:
    """Loopback-only authenticated HTTP projection of one frozen tool backend."""

    def __init__(
        self,
        backend: HarnessMemoryBackend,
        *,
        run_id: str,
        task_id: str,
        condition: str,
        cache_phase: CachePhase,
        allow_writes: bool = False,
    ) -> None:
        self.backend = backend
        self.run_id = run_id
        self.task_id = task_id
        self.condition = condition
        self.cache_phase = cache_phase
        self.allow_writes = allow_writes
        self.token = secrets.token_urlsafe(32)
        self._server = _BridgeServer(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"memoryos-harness-{task_id}",
            daemon=True,
        )
        self._lock = threading.Lock()
        self._events: list[ToolEvent] = []
        self._previous_visible_context_id: str | None = None
        self._context_calls = 0

    @property
    def base_url(self) -> str:
        host, port = cast(tuple[str, int], self._server.server_address)
        return f"http://{host}:{port}"

    @property
    def events(self) -> tuple[ToolEvent, ...]:
        return tuple(self._events)

    def __enter__(self) -> MemoryOSHTTPBridge:
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def handle(self, handler: _BridgeHandler, method: Literal["GET", "POST"]) -> None:
        if not secrets.compare_digest(
            handler.headers.get("Authorization", ""), f"Bearer {self.token}"
        ):
            self._send(handler, 401, {"error": "unauthorized"})
            return
        started = time.perf_counter()
        arguments: dict[str, Any] = {}
        tool = "unknown"
        try:
            parsed = urlparse(handler.path)
            if method == "POST" and parsed.path == "/api/context":
                tool = "memory_context"
                arguments = self._read_json(handler)
                self._validate_context(arguments)
                result = self._execute_context(arguments)
            elif method == "POST" and parsed.path == "/api/memories" and self.allow_writes:
                tool = "memory_propose"
                arguments = self._read_json(handler)
                self._validate_proposal(arguments)
                result = self._execute_write(tool, arguments)
            elif (
                method == "POST"
                and parsed.path.startswith("/api/memories/")
                and parsed.path.endswith("/confirm")
                and self.allow_writes
            ):
                tool = "memory_confirm"
                memory_id = unquote(parsed.path[len("/api/memories/") : -len("/confirm")]).strip(
                    "/"
                )
                if not memory_id:
                    raise ValueError("memory id is required")
                arguments = self._read_json(handler)
                arguments = {"memory_id": memory_id, **arguments}
                result = self._execute_write(tool, arguments)
            elif (
                method == "GET"
                and parsed.path.startswith("/api/memories/")
                and parsed.path.endswith("/explain")
            ):
                tool = "memory_explain"
                memory_id = unquote(parsed.path[len("/api/memories/") : -len("/explain")]).strip(
                    "/"
                )
                arguments = self._explain_arguments(memory_id, parsed.query)
                result = self._execute_explain(arguments)
            else:
                self._send(handler, 404, {"error": "not_found"})
                return
            self._record(tool, arguments, result, started, ok=True)
            self._send(handler, 200, result)
        except Exception as exc:
            payload = {
                "error": type(exc).__name__,
                "message": str(exc)[:2000],
            }
            self._record(tool, arguments, payload, started, ok=False, error_code=type(exc).__name__)
            self._send(handler, 400, payload)

    def _read_json(self, handler: _BridgeHandler) -> dict[str, Any]:
        length_text = handler.headers.get("Content-Length")
        if length_text is None or not length_text.isdigit():
            raise ValueError("Content-Length is required")
        length = int(length_text)
        if length < 2 or length > _MAX_HTTP_BODY:
            raise ValueError("invalid request body length")
        value = json.loads(handler.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return cast(dict[str, Any], value)

    def _validate_context(self, arguments: Mapping[str, Any]) -> None:
        policy = self.backend.policy
        expected_mode = (
            policy.initial_response_mode
            if self._context_calls == 0
            else policy.subsequent_response_mode
        )
        expected_previous = (
            self._previous_visible_context_id
            if policy.use_previous_context and self._context_calls > 0
            else None
        )
        expected = {
            "task": self.backend.task,
            "repository": self.backend.repository,
            "budget_tokens": self.backend.budget_tokens,
            "detail_level": policy.detail_level.value,
            "response_mode": expected_mode,
            **({"previous_context_id": expected_previous} if expected_previous is not None else {}),
        }
        if dict(arguments) != expected:
            mismatch_fields = sorted(
                {
                    *set(arguments).symmetric_difference(expected),
                    *(
                        key
                        for key in set(arguments).intersection(expected)
                        if arguments[key] != expected[key]
                    ),
                }
            )
            raise ValueError(
                "Harness MemoryOS request diverged from the frozen condition policy: "
                + ", ".join(mismatch_fields)
            )

    def _validate_proposal(self, arguments: Mapping[str, Any]) -> None:
        if arguments.get("scope_type") != "repository":
            raise ValueError("cross-session proposals must use repository scope")
        if arguments.get("scope_key") != self.backend.repository:
            raise ValueError("cross-session proposal scope diverged from the frozen repository")
        if arguments.get("created_by") != "agent":
            raise ValueError("cross-session proposals must be attributed to the agent")
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("cross-session proposals require one stable atomic fact key")
        source = arguments.get("source")
        if not isinstance(source, Mapping) or source.get("source_type") != "conversation":
            raise ValueError("cross-session proposals require conversation evidence")
        source_ref = source.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref.startswith(
            "deepseek-harness:session-"
        ):
            raise ValueError("cross-session proposals require the current Harness session source")

    def _execute_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            wrapper = self.backend.execute("memory_context", arguments)
        if not wrapper.get("ok"):
            raise RuntimeError(canonical_json(wrapper.get("error")))
        result = wrapper.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            raise RuntimeError("MemoryOS returned an invalid context wrapper")
        context = cast(dict[str, Any], result["context"])
        self._context_calls += 1
        value = context.get("context_id")
        self._previous_visible_context_id = value if isinstance(value, str) else None
        return context

    @staticmethod
    def _explain_arguments(memory_id: str, query: str) -> dict[str, Any]:
        if not memory_id:
            raise ValueError("memory id is required")
        values = parse_qs(query, keep_blank_values=False)
        display_handle = _MEMORY_DISPLAY_HANDLE.fullmatch(memory_id)
        if display_handle is not None:
            memory_id = display_handle.group("memory_id").lower()
        arguments: dict[str, Any] = {"memory_id": memory_id}
        if "expected_atom_sha256" in values:
            arguments["expected_atom_sha256"] = values["expected_atom_sha256"][-1]
        if display_handle is not None:
            displayed_sha256 = display_handle.group("atom_sha256").lower()
            expected_sha256 = arguments.get("expected_atom_sha256")
            if expected_sha256 is not None and expected_sha256.lower() != displayed_sha256:
                raise ValueError("display handle and expected atom fingerprint disagree")
            arguments["expected_atom_sha256"] = displayed_sha256
        if "sections" in values:
            arguments["sections"] = values["sections"]
        if "budget_tokens" in values:
            budget = values["budget_tokens"][-1]
            if not budget.isdigit():
                raise ValueError("budget_tokens must be an integer")
            arguments["budget_tokens"] = int(budget)
        return arguments

    def _execute_explain(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            wrapper = self.backend.execute("memory_explain", arguments)
        if not wrapper.get("ok"):
            raise RuntimeError(canonical_json(wrapper.get("error")))
        result = wrapper.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("MemoryOS returned an invalid explanation")
        return cast(dict[str, Any], result)

    def _execute_write(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            wrapper = self.backend.execute(name, arguments)
        if not wrapper.get("ok"):
            raise RuntimeError(canonical_json(wrapper.get("error")))
        result = wrapper.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"MemoryOS returned an invalid {name} result")
        return cast(dict[str, Any], result)

    def _record(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        result: Any,
        started: float,
        *,
        ok: bool,
        error_code: str | None = None,
    ) -> None:
        event_index = len(self._events)
        self._events.append(
            ToolEvent(
                run_id=self.run_id,
                task_id=self.task_id,
                condition=self.condition,
                cache_phase=self.cache_phase,
                session_id="deepseek-harness-http-bridge",
                step_index=event_index,
                event_index=event_index,
                tool=tool,
                category="memory" if tool.startswith("memory_") else "unknown",
                arguments_sha256=_sha256_json(dict(arguments)),
                result_sha256=_sha256_json(result),
                ok=ok,
                duration_seconds=round(time.perf_counter() - started, 6),
                blocked=not ok,
                error_code=error_code,
            )
        )

    @staticmethod
    def _send(handler: _BridgeHandler, status: int, payload: Mapping[str, Any]) -> None:
        encoded = canonical_json(dict(payload)).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)


class DeepSeekHarnessCodingAgent:
    """Run one pinned headless Harness session in an isolated DSH_HOME."""

    def __init__(
        self,
        runtime: DeepSeekHarnessRuntime,
        *,
        project_root: Path | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.runtime = runtime
        self.project_root = (
            project_root.resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.which = which

    def run(
        self,
        *,
        workspace: Path,
        memory_tools: HarnessMemoryBackend | None,
        state_dir: Path,
        harness_home: Path,
        filesystem_root: Path,
        task: str,
        repository: str,
        run_id: str,
        task_id: str,
        condition: str,
        cache_phase: CachePhase,
        cache_namespace: str,
        budget_tokens: int,
        resume_session_id: str | None = None,
        prompt_override: str | None = None,
        memory_tool_profile: Literal["read-only", "cross-session-write"] = "read-only",
        usage_guard_file: Path | None = None,
        evaluation_history_char_limit: int | None = None,
        evaluation_sentinel: str | None = None,
        enforce_budget: bool = True,
    ) -> CodingAgentResult:
        if resume_session_id is not None and prompt_override is None:
            return _failed(
                "harness_resume_configuration_error",
                "resume_session_id requires prompt_override",
            )
        if resume_session_id is not None and not _HARNESS_SESSION_ID.fullmatch(resume_session_id):
            return _failed(
                "harness_resume_configuration_error",
                "resume_session_id is not a valid Harness session UUID",
            )
        if prompt_override is not None and not prompt_override.strip():
            return _failed(
                "harness_resume_configuration_error",
                "prompt_override cannot be blank",
            )
        if memory_tool_profile == "cross-session-write" and memory_tools is None:
            return _failed(
                "harness_write_profile_configuration_error",
                "cross-session-write requires a MemoryOS backend",
            )
        if evaluation_history_char_limit is not None and not (
            1_024 <= evaluation_history_char_limit <= 10_000_000
        ):
            return _failed(
                "harness_evaluation_window_configuration_error",
                "evaluation_history_char_limit must be between 1024 and 10000000",
            )
        if evaluation_sentinel is not None and (
            not evaluation_sentinel or len(evaluation_sentinel) > 10_000
        ):
            return _failed(
                "harness_evaluation_window_configuration_error",
                "evaluation_sentinel must contain between 1 and 10000 characters",
            )
        executable = self._resolve_executable()
        if executable is None:
            return _blocked("DeepSeek Harness executable is unavailable")
        api_key = os.environ.get(self.runtime.api_key_environment)
        if not api_key:
            return _blocked(f"{self.runtime.api_key_environment} is unavailable")
        plugin = Path(self.runtime.plugin_path)
        if not plugin.is_absolute():
            plugin = self.project_root / plugin
        plugin = plugin.resolve()
        if not (plugin / "package.json").is_file() or not (plugin / "cordis.patch.yml").is_file():
            return _failed("harness_plugin_missing", f"invalid Harness plugin directory: {plugin}")

        state = state_dir.resolve()
        state.mkdir(parents=True, exist_ok=False)
        dsh_home = harness_home.resolve()
        dsh_home.mkdir(parents=True, exist_ok=True)
        sandbox_root = filesystem_root.resolve()
        resolved_usage_guard = (
            usage_guard_file.resolve(strict=True) if usage_guard_file is not None else None
        )
        try:
            _validate_filesystem_root(
                sandbox_root,
                workspace.resolve(),
                dsh_home,
                state,
                *(() if resolved_usage_guard is None else (resolved_usage_guard,)),
            )
        except ValueError as exc:
            return _blocked(str(exc))
        _freeze_cache_identity(dsh_home, cache_namespace)
        _freeze_harness_settings(dsh_home, self.runtime)
        _freeze_harness_request_controls(dsh_home)
        _freeze_offline_agent_preset(dsh_home, self.runtime)
        shell_launcher = Path(self.runtime.tool_shell_launcher)
        if not shell_launcher.is_file() or not os.access(shell_launcher, os.X_OK):
            return _blocked("the frozen no-network Harness shell launcher is unavailable")
        filesystem_launcher = Path(self.runtime.agent_filesystem_launcher)
        if not filesystem_launcher.is_file() or not os.access(filesystem_launcher, os.X_OK):
            return _blocked("the frozen Harness filesystem launcher is unavailable")
        usage_path = state / "provider-usage.jsonl"
        attempt_path = state / "provider-attempts.jsonl"
        environment = _harness_environment(
            dsh_home,
            api_key,
            self.runtime.base_url,
            permission_mode=self.runtime.permission_mode,
            shell_launcher=shell_launcher,
            filesystem_root=sandbox_root,
        )
        if resume_session_id is not None:
            environment["MEMORYOS_RESUME_SESSION_ID"] = resume_session_id
        version = self._command(
            [executable, "--version"],
            cwd=workspace,
            environment=environment,
            timeout_seconds=30,
        )
        if version is None:
            return _blocked("DeepSeek Harness version check could not start")
        if version.returncode != 0 or self.runtime.harness_version not in (
            version.stdout + version.stderr
        ):
            found = (version.stdout + version.stderr).strip()[:500] or "unknown"
            return _blocked(
                f"DeepSeek Harness {self.runtime.harness_version} is required; found: {found}"
            )

        install_marker = dsh_home / f".memoryos-plugin-{MEMORYOS_PLUGIN_VERSION}-installed"
        if not install_marker.is_file():
            pnpm = self.which("pnpm")
            if pnpm is None:
                return _blocked("pnpm is required to package the DeepSeek Harness plugin")
            pnpm_version = self._command(
                [pnpm, "--version"],
                cwd=plugin,
                environment=environment,
                timeout_seconds=30,
            )
            if (
                pnpm_version is None
                or pnpm_version.returncode != 0
                or pnpm_version.stdout.strip() != self.runtime.pnpm_version
            ):
                found = (
                    _process_message(pnpm_version) if pnpm_version is not None else "unavailable"
                )
                return _blocked(f"pnpm {self.runtime.pnpm_version} is required; found: {found}")
            package_dir = state / "plugin-package"
            package_dir.mkdir()
            packed = self._command(
                [pnpm, "pack", "--pack-destination", str(package_dir)],
                cwd=plugin,
                environment=environment,
                timeout_seconds=self.runtime.install_timeout_seconds,
            )
            if packed is None:
                return _blocked("DeepSeek Harness plugin packaging could not start")
            _write_process_logs(state, "pack", packed)
            tarballs = sorted(package_dir.glob("*.tgz"))
            if packed.returncode != 0 or len(tarballs) != 1:
                return _blocked(
                    "DeepSeek Harness plugin packaging failed: " + _process_message(packed)
                )
            installed = self._command(
                [
                    executable,
                    "plugin",
                    "--profile",
                    self.runtime.profile,
                    "add",
                    str(tarballs[0]),
                ],
                cwd=workspace,
                environment=environment,
                timeout_seconds=self.runtime.install_timeout_seconds,
            )
            if installed is None:
                return _blocked("DeepSeek Harness plugin installation could not start")
            _write_process_logs(state, "install", installed)
            if installed.returncode != 0:
                return _blocked(
                    "DeepSeek Harness plugin installation failed: " + _process_message(installed)
                )
            package_sha256 = hashlib.sha256(tarballs[0].read_bytes()).hexdigest()
            install_marker.write_text(
                f"{plugin}\n{package_sha256}\n", encoding="utf-8", newline="\n"
            )

        memory_enabled = memory_tools is not None
        if memory_enabled == (condition == "no_memory"):
            return _failed(
                "harness_condition_mismatch",
                "no_memory must disable the MemoryOS tool component and memory conditions "
                "must enable it",
            )
        if memory_tools is not None and memory_tools.budget_tokens != budget_tokens:
            return _failed(
                "harness_memory_budget_mismatch",
                "MemoryOS backend and Harness plugin context budgets must match",
            )
        if (
            self.runtime.agent_preset in _DEEPSEEK_COMPACT_PRESETS
            and memory_enabled
            and condition != "msc_context_only"
        ):
            return _failed(
                "deepseek_optimized_condition_mismatch",
                "the DeepSeek optimized preset requires msc_context_only so the model "
                "receives one complete context response without an explain round trip",
            )
        prompt = (
            prompt_override
            if prompt_override is not None
            else harness_headless_task(
                repository,
                task,
                agent_preset=self.runtime.agent_preset,
            )
        )
        harness_command = _isolated_harness_command(
            filesystem_launcher=filesystem_launcher,
            executable=executable,
            filesystem_root=sandbox_root,
        )
        budget_probe = (
            _deepseek_budget_probe(
                usage_path,
                workspace,
                attempt_path=attempt_path,
                no_patch_request_limit=self.runtime.no_patch_request_limit,
                no_patch_input_token_limit=(self.runtime.no_patch_input_token_limit),
                no_patch_output_token_limit=(self.runtime.no_patch_output_token_limit),
                patch_preserving_request_limit=(self.runtime.patch_preserving_request_limit),
                patch_preserving_input_token_limit=(
                    self.runtime.patch_preserving_input_token_limit
                ),
                patch_preserving_output_token_limit=(
                    self.runtime.patch_preserving_output_token_limit
                ),
                hard_request_limit=self.runtime.hard_request_limit,
                hard_input_token_limit=self.runtime.hard_input_token_limit,
                hard_output_token_limit=self.runtime.hard_output_token_limit,
                progress_grace_requests=self.runtime.progress_grace_requests,
            )
            if enforce_budget and self.runtime.agent_preset in _DEEPSEEK_BUDGETED_PRESETS
            else None
        )
        if memory_tools is None:
            environment.update(
                _plugin_environment(
                    self.runtime,
                    None,
                    usage_path=usage_path,
                    attempt_path=attempt_path,
                    task=task,
                    repository=repository,
                    run_id=run_id,
                    task_id=task_id,
                    condition=condition,
                    cache_phase=cache_phase,
                    cache_namespace=cache_namespace,
                    budget_tokens=budget_tokens,
                    tool_profile=memory_tool_profile,
                    usage_guard_file=resolved_usage_guard,
                    evaluation_history_char_limit=evaluation_history_char_limit,
                    evaluation_eviction_output_file=(
                        state / "controlled-context-evictions.jsonl"
                        if evaluation_history_char_limit is not None
                        else None
                    ),
                    evaluation_sentinel=evaluation_sentinel,
                )
            )
            completed = self._command(
                [*harness_command, "--profile", self.runtime.profile, prompt],
                cwd=workspace,
                environment=environment,
                timeout_seconds=self.runtime.run_timeout_seconds,
                budget_probe=budget_probe,
            )
            events: tuple[ToolEvent, ...] = ()
        else:
            with MemoryOSHTTPBridge(
                memory_tools,
                run_id=run_id,
                task_id=task_id,
                condition=condition,
                cache_phase=cache_phase,
                allow_writes=memory_tool_profile == "cross-session-write",
            ) as bridge:
                environment.update(
                    _plugin_environment(
                        self.runtime,
                        bridge,
                        usage_path=usage_path,
                        attempt_path=attempt_path,
                        task=task,
                        repository=repository,
                        run_id=run_id,
                        task_id=task_id,
                        condition=condition,
                        cache_phase=cache_phase,
                        cache_namespace=cache_namespace,
                        budget_tokens=budget_tokens,
                        tool_profile=memory_tool_profile,
                        usage_guard_file=resolved_usage_guard,
                        evaluation_history_char_limit=evaluation_history_char_limit,
                        evaluation_eviction_output_file=(
                            state / "controlled-context-evictions.jsonl"
                            if evaluation_history_char_limit is not None
                            else None
                        ),
                        evaluation_sentinel=evaluation_sentinel,
                    )
                )
                completed = self._command(
                    [*harness_command, "--profile", self.runtime.profile, prompt],
                    cwd=workspace,
                    environment=environment,
                    timeout_seconds=self.runtime.run_timeout_seconds,
                    budget_probe=budget_probe,
                )
                events = bridge.events
        if completed is None:
            return CodingAgentResult(
                status=AgentRunStatus.EXTERNAL_BLOCKER,
                message="DeepSeek Harness process could not start",
                failure_reason="external_blocker",
                tool_events=events,
                steps=0,
                tests_run=0,
                patches_applied=0,
            )
        _write_process_logs(state, "run", completed)
        try:
            usage = _read_usage(
                usage_path,
                run_id=run_id,
                task_id=task_id,
                condition=condition,
                cache_phase=cache_phase,
                model=self.runtime.model,
                cache_namespace=cache_namespace,
            )
            provider_attempts = max(len(usage), _provider_attempt_count(attempt_path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return CodingAgentResult(
                status=AgentRunStatus.FAILED,
                message=str(exc)[:4000],
                failure_reason="harness_usage_protocol_error",
                tool_events=events,
                provider_attempts=_provider_attempt_count(attempt_path),
                steps=0,
                tests_run=0,
                patches_applied=0,
            )
        message = completed.stdout.strip()[-4000:] or _process_message(completed)
        budget_exhausted = completed.returncode == _BUDGET_RETURN_CODE
        usage_guard_stopped = _USAGE_GUARD_STOP_MARKER in (completed.stdout + completed.stderr)
        patch_preserved = (budget_exhausted or usage_guard_stopped) and _workspace_has_changes(
            workspace
        )
        if budget_exhausted:
            message = (
                f"{message}\nDeepSeek request budget reached; the working-tree patch was "
                f"{'preserved for scoring' if patch_preserved else 'empty'}."
            ).strip()[-4000:]
        if usage_guard_stopped:
            message = (
                f"{message}\nController relative-usage guard stopped the run before the "
                f"next provider dispatch; the working-tree patch was "
                f"{'preserved for scoring' if patch_preserved else 'empty'}."
            ).strip()[-4000:]
        if (completed.returncode == 0 or patch_preserved) and usage:
            return CodingAgentResult(
                status=AgentRunStatus.COMPLETED,
                message=message,
                usage=usage,
                tool_events=events,
                provider_attempts=provider_attempts,
                steps=len(usage),
                tests_run=0,
                patches_applied=0,
            )
        external = _looks_external(completed)
        timed_out = completed.returncode == _TIMEOUT_RETURN_CODE
        return CodingAgentResult(
            status=(AgentRunStatus.EXTERNAL_BLOCKER if external else AgentRunStatus.FAILED),
            message=message,
            failure_reason=(
                "external_blocker"
                if external
                else "harness_timeout"
                if timed_out
                else "harness_budget_exhausted"
                if budget_exhausted
                else "relative_usage_guard"
                if usage_guard_stopped
                else "harness_failed"
            ),
            usage=usage,
            tool_events=events,
            provider_attempts=provider_attempts,
            steps=len(usage),
            tests_run=0,
            patches_applied=0,
        )

    def _resolve_executable(self) -> str | None:
        candidate = Path(self.runtime.executable)
        if candidate.is_absolute() or candidate.parent != Path("."):
            return str(candidate.resolve()) if candidate.is_file() else None
        return self.which(self.runtime.executable)

    @staticmethod
    def _command(
        command: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout_seconds: int,
        budget_probe: Callable[[], str | None] | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            process = subprocess.Popen(  # noqa: S603 - direct argv from frozen runtime
                command,
                cwd=cwd,
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
        except OSError:
            return None
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(process, force=False)
                try:
                    stdout, stderr = process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process, force=True)
                    stdout, stderr = process.communicate()
                marker = f"process timed out after {timeout_seconds} seconds"
                stderr = f"{stderr.rstrip()}\n{marker}\n" if stderr else marker + "\n"
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=_TIMEOUT_RETURN_CODE,
                    stdout=stdout,
                    stderr=stderr,
                )
            try:
                stdout, stderr = process.communicate(timeout=min(_BUDGET_POLL_SECONDS, remaining))
            except subprocess.TimeoutExpired:
                budget_reason = budget_probe() if budget_probe is not None else None
                if budget_reason is None:
                    continue
                _terminate_process_tree(process, force=False)
                try:
                    stdout, stderr = process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process, force=True)
                    stdout, stderr = process.communicate()
                marker = f"process stopped by frozen DeepSeek budget: {budget_reason}"
                stderr = f"{stderr.rstrip()}\n{marker}\n" if stderr else marker + "\n"
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=_BUDGET_RETURN_CODE,
                    stdout=stdout,
                    stderr=stderr,
                )
            break
        return subprocess.CompletedProcess(
            args=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def _deepseek_budget_probe(
    usage_path: Path,
    workspace: Path,
    *,
    attempt_path: Path | None = None,
    no_patch_request_limit: int = _DEEPSEEK_NO_PATCH_MAX_REQUESTS,
    no_patch_input_token_limit: int = _DEEPSEEK_NO_PATCH_MAX_INPUT_TOKENS,
    no_patch_output_token_limit: int = _DEEPSEEK_NO_PATCH_MAX_OUTPUT_TOKENS,
    patch_preserving_request_limit: int = _DEEPSEEK_SOFT_MAX_REQUESTS,
    patch_preserving_input_token_limit: int = _DEEPSEEK_SOFT_MAX_INPUT_TOKENS,
    patch_preserving_output_token_limit: int = _DEEPSEEK_SOFT_MAX_OUTPUT_TOKENS,
    hard_request_limit: int = _DEEPSEEK_HARD_MAX_REQUESTS,
    hard_input_token_limit: int = _DEEPSEEK_HARD_MAX_INPUT_TOKENS,
    hard_output_token_limit: int = _DEEPSEEK_HARD_MAX_OUTPUT_TOKENS,
    progress_grace_requests: int = _DEEPSEEK_PROGRESS_GRACE_REQUESTS,
) -> Callable[[], str | None]:
    """Apply phase budgets while preserving a patch that is still changing."""

    observed_usage_requests = -1
    next_workspace_scan_at = 0.0
    patch_fingerprint: str | None = None
    last_patch_progress_attempt = 0

    def probe() -> str | None:
        nonlocal observed_usage_requests
        nonlocal next_workspace_scan_at
        nonlocal patch_fingerprint
        nonlocal last_patch_progress_attempt

        usage_requests, input_tokens, output_tokens = _usage_budget_totals(usage_path)
        provider_attempts = max(
            usage_requests,
            _provider_attempt_count(attempt_path) if attempt_path is not None else 0,
        )
        now = time.monotonic()
        if usage_requests != observed_usage_requests or now >= next_workspace_scan_at:
            current_fingerprint = _workspace_change_fingerprint(workspace)
            if current_fingerprint != patch_fingerprint:
                patch_fingerprint = current_fingerprint
                if current_fingerprint is not None:
                    last_patch_progress_attempt = provider_attempts
            observed_usage_requests = usage_requests
            next_workspace_scan_at = now + 1.0

        hard_reason = _budget_reason(
            provider_attempts,
            input_tokens,
            output_tokens,
            max_requests=hard_request_limit,
            max_input_tokens=hard_input_token_limit,
            max_output_tokens=hard_output_token_limit,
            request_label="provider_attempts",
        )
        if hard_reason is not None:
            return "hard ceiling; " + hard_reason

        if patch_fingerprint is None:
            no_patch_reason = _budget_reason(
                provider_attempts,
                input_tokens,
                output_tokens,
                max_requests=no_patch_request_limit,
                max_input_tokens=no_patch_input_token_limit,
                max_output_tokens=no_patch_output_token_limit,
                request_label="provider_attempts",
            )
            if no_patch_reason is not None:
                return "no-patch ceiling; " + no_patch_reason
            return None

        soft_reason = _budget_reason(
            provider_attempts,
            input_tokens,
            output_tokens,
            max_requests=patch_preserving_request_limit,
            max_input_tokens=patch_preserving_input_token_limit,
            max_output_tokens=patch_preserving_output_token_limit,
            request_label="provider_attempts",
        )
        if (
            soft_reason is not None
            and provider_attempts - last_patch_progress_attempt >= progress_grace_requests
        ):
            return "patch-stagnation soft ceiling; " + soft_reason
        return None

    return probe


def _budget_reason(
    requests: int,
    input_tokens: int,
    output_tokens: int,
    *,
    max_requests: int,
    max_input_tokens: int,
    max_output_tokens: int,
    request_label: str = "requests",
) -> str | None:
    reasons: list[str] = []
    if requests >= max_requests:
        reasons.append(f"{request_label}={requests}/{max_requests}")
    if input_tokens >= max_input_tokens:
        reasons.append(f"input_tokens={input_tokens}/{max_input_tokens}")
    if output_tokens >= max_output_tokens:
        reasons.append(f"output_tokens={output_tokens}/{max_output_tokens}")
    return ", ".join(reasons) if reasons else None


def _usage_budget_totals(path: Path) -> tuple[int, int, int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0, 0, 0
    requests = 0
    input_tokens = 0
    output_tokens = 0
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        tokens = value.get("input_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            requests += 1
            input_tokens += tokens
            output = value.get("output_tokens")
            if isinstance(output, int) and not isinstance(output, bool) and output >= 0:
                output_tokens += output
    return requests, input_tokens, output_tokens


def _provider_attempt_count(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    attempts = 0
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "provider_attempt":
            attempts += 1
    return attempts


def _workspace_change_fingerprint(workspace: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        status = subprocess.run(  # noqa: S603 - fixed read-only git query
            [
                git,
                "-c",
                "core.fileMode=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
            cwd=workspace,
            capture_output=True,
            text=False,
            timeout=5,
            check=False,
        )
        if status.returncode != 0 or not status.stdout.strip():
            return None
        diff = subprocess.run(  # noqa: S603 - fixed read-only git query
            [
                git,
                "-c",
                "core.fileMode=false",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--",
            ],
            cwd=workspace,
            capture_output=True,
            text=False,
            timeout=5,
            check=False,
        )
        cached = subprocess.run(  # noqa: S603 - fixed read-only git query
            [
                git,
                "-c",
                "core.fileMode=false",
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--",
            ],
            cwd=workspace,
            capture_output=True,
            text=False,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if diff.returncode != 0 or cached.returncode != 0:
        return None
    return hashlib.sha256(status.stdout + b"\0" + diff.stdout + b"\0" + cached.stdout).hexdigest()


def _workspace_has_changes(workspace: Path) -> bool:
    return _workspace_change_fingerprint(workspace) is not None


def _terminate_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    """Terminate the isolated command process group without touching unrelated work."""

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            subprocess.run(  # noqa: S603 - fixed Windows process-tree terminator argv
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=_TERMINATION_GRACE_SECONDS,
            )
        elif process.poll() is None:
            process.kill()
        return
    kill_process_group = getattr(os, "killpg", None)
    if kill_process_group is None:
        if process.poll() is None:
            process.kill() if force else process.terminate()
        return
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        kill_process_group(process.pid, sigkill if force else signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is None:
            process.kill() if force else process.terminate()


def _harness_environment(
    dsh_home: Path,
    api_key: str,
    base_url: str,
    *,
    permission_mode: str,
    shell_launcher: Path,
    filesystem_root: Path,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "COREPACK_DEFAULT_TO_LATEST",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT",
            "COREPACK_HOME",
            "HOME",
            "LANG",
            "LC_ALL",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        )
        if name in os.environ
    }
    environment.update(
        {
            "HOME": str(dsh_home),
            "DSH_HOME": str(dsh_home),
            "DSH_TELEMETRY_DISABLED": "1",
            "DEEPSEEK_API_KEY": api_key,
            "DEEPSEEK_BASE_URL": base_url,
            "DSH_PERMISSION_MODE": permission_mode,
            "NO_COLOR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": ("-p no:cacheprovider -W ignore::DeprecationWarning:ast"),
            "MEMORYOS_TOOL_SHELL_NETWORK": "none",
            "MEMORYOS_TOOL_SHELL_READ_SCOPE": "dedicated-condition-mount-system-only",
            "MEMORYOS_AGENT_FILESYSTEM_ROOT": str(filesystem_root),
        }
    )
    path = environment.get("PATH", "")
    environment["PATH"] = str(shell_launcher.parent) + (os.pathsep + path if path else "")
    return environment


def _isolated_harness_command(
    *,
    filesystem_launcher: Path,
    executable: str,
    filesystem_root: Path,
) -> list[str]:
    command = [str(filesystem_launcher)]
    read_only = (
        Path("/bin"),
        Path("/usr"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc"),
        Path("/opt/deepseek-harness"),
        Path("/opt/memoryos/bin"),
        Path("/opt/memoryos/libexec"),
        Path("/opt/memoryos/task-venv"),
    )
    read_write = (
        filesystem_root.resolve(),
        Path(tempfile.gettempdir()),
        Path("/dev/null"),
        Path("/dev/zero"),
        Path("/dev/random"),
        Path("/dev/urandom"),
    )
    for path in read_only:
        if path.exists():
            command.extend(("--ro", str(path)))
    for path in read_write:
        if path.exists():
            command.extend(("--rw", str(path)))
    command.extend(("--", executable))
    return command


def _validate_filesystem_root(root: Path, *controlled_paths: Path) -> None:
    if not root.is_dir():
        raise ValueError("the dedicated Harness filesystem root is unavailable")
    if root == root.parent:
        raise ValueError("the Harness filesystem root cannot be the system root")
    if os.name != "nt" and not root.is_mount():
        raise ValueError("the Harness filesystem root must be a dedicated mountpoint")
    for path in controlled_paths:
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "workspace, Harness home, and state must stay inside the dedicated mountpoint"
            ) from exc


def _freeze_cache_identity(dsh_home: Path, cache_namespace: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", cache_namespace):
        raise ValueError("cache namespace must be a SHA-256 value")
    value = (
        f"{cache_namespace[:8]}-{cache_namespace[8:12]}-{cache_namespace[12:16]}-"
        f"{cache_namespace[16:20]}-{cache_namespace[20:32]}"
    )
    path = dsh_home / ".anonymous-user-id"
    if path.is_file():
        if path.read_text(encoding="utf-8").strip().lower() != value:
            raise ValueError("Harness home is already bound to another cache namespace")
        return
    path.write_text(value + "\n", encoding="utf-8", newline="\n")


def _freeze_harness_settings(dsh_home: Path, runtime: DeepSeekHarnessRuntime) -> None:
    """Bind a fresh Harness home to the experiment's model and agent preset."""

    expected = (
        "agent-presets:\n"
        f"  default: {runtime.agent_preset}\n"
        "agent-default-model:\n"
        f"  provider: {runtime.harness_provider}\n"
        f"  model: {runtime.model}\n"
        f"  reasoningEffort: {runtime.reasoning_effort}\n"
        "llm-deepseek:\n"
        f"  maxTokens: {runtime.max_output_tokens}\n"
        "  models:\n"
        f"    - id: {runtime.model}\n"
        f"      name: {runtime.model}\n"
        f"      contextWindow: {runtime.context_length}\n"
        f"      maxTokens: {runtime.max_output_tokens}\n"
        "  retryPolicy:\n"
        "    mode: normal\n"
        f"    maxRetries: {runtime.provider_retry_limit}\n"
    )
    path = dsh_home / "settings.yaml"
    if path.is_file():
        if path.read_text(encoding="utf-8") != expected:
            raise ValueError("Harness home settings diverge from the frozen experiment runtime")
        return
    path.write_text(expected, encoding="utf-8", newline="\n")


def _freeze_harness_request_controls(dsh_home: Path) -> None:
    """Disable Harness-owned LLM work that is outside the Agent usage ledger."""

    path = dsh_home / "cordis.patch.yml"
    if path.is_file():
        if path.read_text(encoding="utf-8") != _HARNESS_REQUEST_CONTROL_PATCH:
            raise ValueError(
                "Harness home request-control patch diverges from the frozen experiment"
            )
        return
    path.write_text(_HARNESS_REQUEST_CONTROL_PATCH, encoding="utf-8", newline="\n")


def _freeze_offline_agent_preset(
    dsh_home: Path,
    runtime: DeepSeekHarnessRuntime,
    *,
    source: Path = _STANDARD_PRESET_SOURCE,
) -> None:
    if runtime.agent_preset not in {"standard-offline", *_DEEPSEEK_COMPACT_PRESETS}:
        return
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != _STANDARD_PRESET_SOURCE_SHA256:
        raise ValueError("the pinned standard Harness preset source has changed")
    source_text = content.decode("utf-8")
    rendered = (
        _deepseek_optimized_preset_text(
            source_text,
            reuse_existing_apis=(runtime.agent_preset in _DEEPSEEK_BUDGETED_PRESETS),
        )
        if runtime.agent_preset in _DEEPSEEK_COMPACT_PRESETS
        else _standard_offline_preset_text(source_text)
    )
    destination = dsh_home / ".agent-presets" / runtime.agent_preset / "agent.cordis.yml"
    if destination.is_file():
        if destination.read_text(encoding="utf-8") != rendered:
            raise ValueError("the frozen standard-offline Harness preset has changed")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8", newline="\n")


def _standard_offline_preset_text(source: str) -> str:
    delegation = source.find(_DELEGATION_SECTION)
    remaining = source.find(_REMAINING_TOOLS_SECTION, delegation + 1)
    if delegation < 0 or remaining < 0:
        raise ValueError("the standard Harness preset delegation section is unavailable")
    without_delegation = source[:delegation] + source[remaining:]
    web = without_delegation.find(_WEB_TOOL_SECTION)
    if web < 0:
        raise ValueError("the standard Harness preset Web tool section is unavailable")
    rendered = without_delegation[:web].rstrip() + "\n"
    if _HARNESS_ORIGINAL_PERSONA not in rendered:
        raise ValueError("the pinned standard Harness persona has changed")
    rendered = rendered.replace(
        _HARNESS_ORIGINAL_PERSONA,
        _HARNESS_STABLE_PERSONA,
        1,
    )
    if "@deepseek-ai/dsh-tool-web" in rendered or "@deepseek-ai/dsh-tool-subagent" in rendered:
        raise ValueError("the standard-offline Harness preset still exposes external tools")
    if _HARNESS_ORIGINAL_PERSONA in rendered:
        raise ValueError("the standard-offline Harness persona still exposes a volatile cwd")
    return rendered


def _deepseek_optimized_preset_text(
    source: str,
    *,
    reuse_existing_apis: bool = True,
) -> str:
    """Keep only the coding surfaces DeepSeek needs for a bounded offline repair."""

    rendered = _standard_offline_preset_text(source)
    background = rendered.find(_BACKGROUND_JOBS_SECTION)
    compaction = rendered.find(_COMPACTION_SECTION, background + 1)
    if background < 0 or compaction < 0:
        raise ValueError("the standard Harness preset optimization sections are unavailable")
    rendered = rendered[:background] + rendered[compaction:]
    remaining = rendered.find(_REMAINING_TOOLS_SECTION)
    if remaining < 0:
        raise ValueError("the standard Harness preset remaining-tools section is unavailable")
    rendered = rendered[:remaining].rstrip() + "\n"

    concise_persona = (
        "You are a concise coding agent powered by the {{model}} model. "
        "Work only in the assigned isolated workspace. Inspect narrowly, make the smallest "
        "correct patch, "
        + (
            "reuse only APIs and patterns present in the checked-out repository, "
            if reuse_existing_apis
            else ""
        )
        + "follow the explicit task contract when evidence conflicts, run one focused "
        "verification, and stop when the requested change is verified."
    )
    if _HARNESS_STABLE_PERSONA not in rendered:
        raise ValueError("the pinned standard Harness persona has changed")
    rendered = rendered.replace(_HARNESS_STABLE_PERSONA, concise_persona, 1)
    replacements = {
        "thresholdChars: 8192": "thresholdChars: 4096",
        "headChars: 4096": "headChars: 2048",
        "tailChars: 1024": "tailChars: 512",
    }
    for before, after in replacements.items():
        if before not in rendered:
            raise ValueError("the pinned standard Harness compaction policy has changed")
        rendered = rendered.replace(before, after, 1)

    forbidden = (
        "@deepseek-ai/dsh-tool-jobs",
        "@deepseek-ai/dsh-tool-skill",
        "@deepseek-ai/dsh-tool-goal",
        "@deepseek-ai/dsh-plan-mode",
        "@deepseek-ai/dsh-tool-ask-user",
        "@deepseek-ai/dsh-tool-todo",
        "@deepseek-ai/dsh-tool-subagent",
        "@deepseek-ai/dsh-tool-web",
    )
    if any(name in rendered for name in forbidden):
        raise ValueError("the DeepSeek optimized preset still exposes orchestration tools")
    required = (
        "@deepseek-ai/dsh-tool-bash",
        "@deepseek-ai/dsh-tool-fs",
        "@deepseek-ai/dsh-tool-fs-search",
        "@deepseek-ai/dsh-compaction-basic",
    )
    if any(name not in rendered for name in required):
        raise ValueError("the DeepSeek optimized preset lost a required coding surface")
    return rendered


def _plugin_environment(
    runtime: DeepSeekHarnessRuntime,
    bridge: MemoryOSHTTPBridge | None,
    *,
    usage_path: Path,
    attempt_path: Path,
    task: str,
    repository: str,
    run_id: str,
    task_id: str,
    condition: str,
    cache_phase: CachePhase,
    cache_namespace: str,
    budget_tokens: int,
    tool_profile: Literal["read-only", "cross-session-write"] = "read-only",
    usage_guard_file: Path | None = None,
    evaluation_history_char_limit: int | None = None,
    evaluation_eviction_output_file: Path | None = None,
    evaluation_sentinel: str | None = None,
) -> dict[str, str]:
    price = runtime.pricing.find(runtime.provider, runtime.model)
    if price is None:  # pragma: no cover - runtime validation guarantees the row
        raise ValueError("missing frozen DeepSeek price row")
    if price.cache_hit_input_usd_per_million is None:
        raise ValueError("DeepSeek pricing requires an explicit cache-hit price")
    pricing = {
        "cacheMissInputUsdPerMillion": str(price.cache_miss_input_usd_per_million),
        "cacheHitInputUsdPerMillion": str(price.cache_hit_input_usd_per_million),
        "outputUsdPerMillion": str(price.output_usd_per_million),
    }
    environment = {
        "MEMORYOS_ENABLED": "1" if bridge is not None else "0",
        "MEMORYOS_CONDITION": condition,
        "MEMORYOS_USAGE_OUTPUT_FILE": str(usage_path),
        "MEMORYOS_ATTEMPT_OUTPUT_FILE": str(attempt_path),
        "MEMORYOS_RUN_ID": run_id,
        "MEMORYOS_TASK_ID": task_id,
        "MEMORYOS_CACHE_PHASE": cache_phase.value,
        "MEMORYOS_PROVIDER": runtime.provider,
        "MEMORYOS_MODEL": runtime.model,
        "MEMORYOS_CACHE_NAMESPACE_SHA256": cache_namespace,
        "MEMORYOS_PRICING_JSON": canonical_json(pricing),
    }
    if usage_guard_file is not None:
        environment["MEMORYOS_USAGE_GUARD_FILE"] = str(usage_guard_file)
    if evaluation_history_char_limit is not None:
        environment["MEMORYOS_EVAL_HISTORY_CHAR_LIMIT"] = str(evaluation_history_char_limit)
    if evaluation_eviction_output_file is not None:
        environment["MEMORYOS_EVAL_EVICTION_OUTPUT_FILE"] = str(evaluation_eviction_output_file)
    if evaluation_sentinel is not None:
        environment["MEMORYOS_EVAL_SENTINEL"] = evaluation_sentinel
    if bridge is not None:
        deepseek_compact = runtime.agent_preset in _DEEPSEEK_COMPACT_PRESETS
        progressive_compact = condition == "msc_progressive" and not deepseek_compact
        effective_budget_tokens = runtime.effective_memory_budget_tokens(budget_tokens)
        environment.update(
            {
                "MEMORYOS_BASE_URL": bridge.base_url,
                "MEMORYOS_AUTH_TOKEN": bridge.token,
                "MEMORYOS_BUDGET_TOKENS": str(effective_budget_tokens),
                "MEMORYOS_TIMEOUT_MS": str(runtime.memory_timeout_seconds * 1000),
                "MEMORYOS_REPOSITORY": repository,
                "MEMORYOS_TASK": task,
                "MEMORYOS_RESPONSE_FORMAT": (
                    "deepseek-compact"
                    if deepseek_compact
                    else "deepseek-progressive-compact"
                    if progressive_compact
                    else "json"
                ),
                "MEMORYOS_MAX_CONTEXT_CALLS": "1" if deepseek_compact else "0",
                "MEMORYOS_TOOL_PROFILE": tool_profile,
            }
        )
    return environment


def _read_usage(
    path: Path,
    *,
    run_id: str,
    task_id: str,
    condition: str,
    cache_phase: CachePhase,
    model: str,
    cache_namespace: str,
) -> tuple[ProviderUsageRecord, ...]:
    if not path.is_file():
        return ()
    records: list[ProviderUsageRecord] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = ProviderUsageRecord.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"invalid Harness usage record at line {line_number}: {exc}") from exc
        expected = (
            record.run_id == run_id
            and record.task_id == task_id
            and record.condition == condition
            and record.cache_phase is cache_phase
            and record.provider == "deepseek"
            and record.model == model
            and record.cache_namespace_sha256 == cache_namespace
        )
        if not expected:
            raise ValueError(f"Harness usage metadata mismatch at line {line_number}")
        records.append(record)
    if len({record.step_index for record in records}) != len(records):
        raise ValueError("Harness usage contains duplicate step indexes")
    return tuple(sorted(records, key=lambda record: record.step_index))


def harness_headless_task(
    repository: str,
    task: str,
    *,
    agent_preset: str = "minimal",
) -> str:
    if agent_preset == _DEEPSEEK_OPTIMIZED_PRESET:
        suffix = (
            "Work in one bounded pass. If memory_context is available, call it exactly once "
            "before broad repository search; treat its short project context as a hypothesis "
            "and verify it against code. Do not call MemoryOS again. Inspect only the relevant "
            "implementation and adjacent tests. Use evidence only from the checked-out repository; "
            "do not inspect installed copies of the target project, site-packages, package caches, "
            "or other versions, and do not reconstruct an unseen upstream patch. The explicit task "
            "contract and current checkout take precedence over behavior from later releases. "
            "Once you can state a concrete edit, make it in the next tool call; never announce an "
            "edit and then perform another search or read. Begin editing no later than the sixth "
            "repository-inspection tool call. If evidence is still incomplete then, use at most "
            "one final targeted read rather than tracing broader call paths. Do not keep exploring "
            "merely to remove every uncertainty. Make the smallest correct patch, run one focused "
            "verification, and inspect only that result. If it fails for a reason caused by your "
            "change, make one targeted correction and verify once more; otherwise finish. "
            "If the test imports the target package, first confirm that its resolved path is "
            "inside the current workspace. Do not run the full repository test suite unless no "
            "focused test can validate the change. Do not repeat an unrelated environment "
            "failure. Leave the final modifications in the working tree. Do not use web search "
            "or downloads."
        )
    elif agent_preset == _DEEPSEEK_OPTIMIZED_PRESET_V2:
        suffix = (
            "Work in one bounded pass. If memory_context is available, call it exactly once "
            "before broad repository search; treat its short project context as a hypothesis "
            "and verify it against code. Do not call MemoryOS again. Inspect only the relevant "
            "implementation and adjacent tests. Use evidence only from the checked-out repository; "
            "do not inspect installed copies of the target project, site-packages, package caches, "
            "or other versions, and do not reconstruct an unseen upstream patch. The explicit task "
            "contract and current checkout take precedence over behavior from later releases. "
            "Make the smallest correct patch, run one focused verification, and then finish; "
            "if the test imports the target package, first confirm that its resolved path is "
            "inside the current workspace. Do not run the full repository test suite unless no "
            "focused test can validate the change. Do not repeat an unrelated environment "
            "failure. Leave the final modifications in the working tree. Do not use web search "
            "or downloads."
        )
    elif agent_preset == _DEEPSEEK_OPTIMIZED_PRESET_V1:
        suffix = (
            "Work in one bounded pass. If memory_context is available, call it exactly once "
            "before broad repository search; treat its short project context as a hypothesis "
            "and verify it against code. Do not call MemoryOS again. Inspect only the relevant "
            "implementation and adjacent tests, make the smallest correct patch, and run focused "
            "tests once. Do not run the full repository test suite unless no focused test can "
            "validate the change. Do not repeat an unrelated environment failure. Leave the "
            "final modifications in the working tree. Do not use web search or downloads."
        )
    else:
        suffix = (
            "If memory_context is available, call it before inspecting code. Use memory_explain "
            "only when that tool is available and the returned index or delta needs evidence. "
            "When available context metadata identifies a delta condition, call memory_context "
            "again after the initial inspection or edit to exercise full-to-delta. Inspect the "
            "workspace, implement the change, run relevant tests, and leave the final "
            "modifications in the working tree. Do not use web search."
        )
    return f"Repository: {repository}\n\nTask:\n{task.strip()}\n\n" + suffix


def _blocked(message: str) -> CodingAgentResult:
    return CodingAgentResult(
        status=AgentRunStatus.EXTERNAL_BLOCKER,
        message=message,
        failure_reason="external_blocker",
        steps=0,
        tests_run=0,
        patches_applied=0,
    )


def _failed(code: str, message: str) -> CodingAgentResult:
    return CodingAgentResult(
        status=AgentRunStatus.FAILED,
        message=message,
        failure_reason=code,
        steps=0,
        tests_run=0,
        patches_applied=0,
    )


def _looks_external(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == _TIMEOUT_RETURN_CODE:
        return False
    value = (result.stdout + "\n" + result.stderr).lower()
    return any(
        marker in value
        for marker in (
            "api key",
            "authentication",
            "connection refused",
            "connection reset",
            "econnrefused",
            "enotfound",
            "network",
            "timed out",
            "timeout",
        )
    )


def _process_message(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}")[
        -4000:
    ]


def _write_process_logs(state: Path, prefix: str, result: subprocess.CompletedProcess[str]) -> None:
    (state / f"{prefix}-stdout.log").write_text(
        result.stdout[-_MAX_LOG_CHARS:], encoding="utf-8", newline="\n"
    )
    (state / f"{prefix}-stderr.log").write_text(
        result.stderr[-_MAX_LOG_CHARS:], encoding="utf-8", newline="\n"
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "HARNESS_COMMIT",
    "HARNESS_VERSION",
    "DeepSeekHarnessCodingAgent",
    "DeepSeekHarnessRuntime",
    "MemoryOSHTTPBridge",
    "_freeze_harness_settings",
    "harness_headless_task",
]
