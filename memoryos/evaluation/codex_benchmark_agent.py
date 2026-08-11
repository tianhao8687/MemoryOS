from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

_CODEX_EXECUTABLE = Path("/usr/local/bin/codex")
_CREDENTIAL_ROOT = Path("/run/credentials")
_CODEX_HOME = Path("/home/agent/.codex")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BENCHMARK_MCP_NAME = "benchmark_memory"
_BENCHMARK_MCP_TOOL = "memory_context"
_BENCHMARK_MCP_URL = "http://benchmark-memory:8000/mcp"
_APP_SERVER_TOOL_ITEM_TYPES = {
    "collabToolCall",
    "commandExecution",
    "dynamicToolCall",
    "fileChange",
    "mcpToolCall",
    "webSearch",
}
_GRANULAR_APPROVAL_POLICY = {
    "granular": {
        "mcp_elicitations": True,
        "request_permissions": False,
        "rules": False,
        "sandbox_approval": False,
        "skill_approval": False,
    }
}
_STREAM_END = object()


@dataclass
class CodexEventSummary:
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int = 0
    message: str | None = None
    turn_completed: bool = False
    failed: bool = False

    def consume(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage")
            total = token_usage.get("total") if isinstance(token_usage, dict) else None
            if isinstance(total, dict):
                self.input_tokens = _nonnegative_int(total.get("inputTokens"))
                self.output_tokens = _nonnegative_int(total.get("outputTokens"))
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                self._consume_completed_item(item)
            return
        if method == "error":
            self.failed = True
            return
        if method != "turn/completed":
            return
        turn = params.get("turn")
        if not isinstance(turn, dict):
            self.failed = True
            return
        self.turn_completed = True
        if turn.get("status") != "completed":
            self.failed = True
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    self._consume_agent_message(item)

    def _consume_completed_item(self, item: dict[str, Any]) -> None:
        item_type = item.get("type")
        if item_type in _APP_SERVER_TOOL_ITEM_TYPES:
            self.tool_calls += 1
        if item_type == "agentMessage":
            self._consume_agent_message(item)

    def _consume_agent_message(self, item: dict[str, Any]) -> None:
        if item.get("phase") == "commentary":
            return
        if isinstance(item.get("text"), str):
            self.message = _bounded_message(item["text"])


@dataclass
class _AppServerState:
    expected_memory_arguments: dict[str, Any]
    summary: CodexEventSummary = field(default_factory=CodexEventSummary)
    pending_mcp_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    protocol_error: str | None = None

    def consume_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if method == "item/started" and isinstance(params, dict):
            item = params.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "mcpToolCall"
                and isinstance(item.get("id"), str)
            ):
                self.pending_mcp_calls[item["id"]] = item
        self.summary.consume(message)
        if method == "item/completed" and isinstance(params, dict):
            item = params.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self.pending_mcp_calls.pop(item["id"], None)

    def approve_memory_elicitation(self, params: Any) -> bool:
        if not isinstance(params, dict):
            return self._deny("mcp_params_not_object")
        if params.get("serverName") != _BENCHMARK_MCP_NAME:
            return self._deny("mcp_server_mismatch")
        if params.get("mode") != "form":
            return self._deny("mcp_elicitation_mode_mismatch")
        if params.get("requestedSchema") != {"type": "object", "properties": {}}:
            return self._deny("mcp_approval_schema_mismatch")
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return self._deny("mcp_approval_meta_missing")
        if meta.get("codex_approval_kind") != "mcp_tool_call":
            return self._deny("mcp_approval_kind_mismatch")
        tool_params = meta.get("tool_params")
        if not _valid_memory_arguments(tool_params):
            return self._deny("mcp_tool_params_invalid")
        matching_calls = [
            call
            for call in self.pending_mcp_calls.values()
            if call.get("server") == _BENCHMARK_MCP_NAME
            and call.get("tool") == _BENCHMARK_MCP_TOOL
            and call.get("arguments") == tool_params
        ]
        if len(matching_calls) != 1:
            return self._deny("mcp_pending_call_mismatch")
        if tool_params != self.expected_memory_arguments:
            self.protocol_error = self.protocol_error or "memory_arguments_mismatch"
            self.summary.failed = True
        return True

    def _deny(self, code: str) -> bool:
        self.protocol_error = self.protocol_error or code
        return False


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _bounded_message(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:2000]


def _valid_memory_arguments(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"repo", "task", "budget"}:
        return False
    repository = value.get("repo")
    task = value.get("task")
    budget = value.get("budget")
    return (
        isinstance(repository, str)
        and 0 < len(repository) <= 256
        and isinstance(task, str)
        and 0 < len(task) <= 100_000
        and not isinstance(budget, bool)
        and budget == 6000
    )


def _expected_memory_arguments(prompt: str) -> dict[str, Any]:
    repository_prefix = "Repository scope: "
    task_marker = "\n\nTask:\n"
    protocol_marker = "\n\nMandatory benchmark tool protocol:\n"
    if not prompt.startswith(repository_prefix):
        raise ValueError("benchmark prompt has no repository scope")
    repository_and_task = prompt[len(repository_prefix) :]
    repository, separator, remainder = repository_and_task.partition(task_marker)
    if not separator:
        raise ValueError("benchmark prompt has no task marker")
    task, separator, _ = remainder.partition(protocol_marker)
    repository = repository.strip()
    task = task.strip()
    if not separator or not repository or not task:
        raise ValueError("benchmark prompt structure is invalid")
    return {"repo": repository, "task": task, "budget": 6000}


def _mcp_overrides(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid benchmark MCP configuration") from exc
    if not isinstance(payload, dict) or set(payload) != {"mcpServers"}:
        raise ValueError("benchmark MCP configuration must contain only mcpServers")
    servers = payload["mcpServers"]
    if not isinstance(servers, dict):
        raise ValueError("benchmark mcpServers must be an object")
    if not servers:
        return []
    if set(servers) != {_BENCHMARK_MCP_NAME}:
        raise ValueError("benchmark MCP configuration contains an unexpected server")
    config = servers[_BENCHMARK_MCP_NAME]
    if not isinstance(config, dict) or set(config) != {"transport", "url"}:
        raise ValueError("benchmark MCP server configuration has unexpected fields")
    if config.get("transport") != "streamable-http" or not isinstance(config.get("url"), str):
        raise ValueError("benchmark MCP server must use streamable-http")
    url = config["url"]
    parsed = urlsplit(url)
    if (
        url != _BENCHMARK_MCP_URL
        or parsed.scheme != "http"
        or parsed.hostname != "benchmark-memory"
        or parsed.port != 8000
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("benchmark MCP URL must target the isolated memory sidecar")
    prefix = f"mcp_servers.{_BENCHMARK_MCP_NAME}"
    return [
        "-c",
        f"{prefix}.url={json.dumps(url)}",
        "-c",
        f"{prefix}.required=true",
        "-c",
        f'{prefix}.enabled_tools=["{_BENCHMARK_MCP_TOOL}"]',
        "-c",
        f'{prefix}.default_tools_approval_mode="prompt"',
        "-c",
        f'{prefix}.tools.{_BENCHMARK_MCP_TOOL}.approval_mode="prompt"',
    ]


def _prepare_codex_home(auth_file: Path) -> None:
    if auth_file.is_symlink():
        raise ValueError("Codex authentication must be a small regular file")
    resolved = auth_file.resolve(strict=True)
    try:
        resolved.relative_to(_CREDENTIAL_ROOT)
    except ValueError as exc:
        raise ValueError("Codex authentication must come from /run/credentials") from exc
    if not resolved.is_file() or resolved.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Codex authentication must be a small regular file")
    try:
        auth_payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex authentication is not valid JSON") from exc
    if not isinstance(auth_payload, dict) or auth_payload.get("auth_mode") not in {
        "apikey",
        "chatgpt",
    }:
        raise ValueError("unsupported Codex authentication mode")
    _CODEX_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = _CODEX_HOME / "auth.json"
    shutil.copyfile(resolved, destination)
    destination.chmod(0o600)


def _reject_project_codex_directory(workspace: Path) -> None:
    project_codex = workspace / ".codex"
    if project_codex.exists() or project_codex.is_symlink():
        raise ValueError("project-local .codex content is forbidden in benchmark workspaces")


def _app_server_command(arguments: argparse.Namespace, mcp_config: Path) -> list[str]:
    if not _MODEL_NAME.fullmatch(arguments.model):
        raise ValueError("unsafe Codex model name")
    return [
        str(_CODEX_EXECUTABLE),
        "app-server",
        "--stdio",
        "--strict-config",
        "--enable",
        "tool_call_mcp_elicitation",
        "-c",
        "analytics.enabled=false",
        "-c",
        "agents.enabled=false",
        "-c",
        "memories.generate_memories=false",
        "-c",
        f"model={json.dumps(arguments.model)}",
        "-c",
        f"model_reasoning_effort={json.dumps(arguments.reasoning_effort)}",
        "-c",
        f"service_tier={json.dumps(arguments.service_tier)}",
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        (
            "approval_policy={granular={sandbox_approval=false,rules=false,"
            "mcp_elicitations=true,request_permissions=false,skill_approval=false}}"
        ),
        *_mcp_overrides(mcp_config),
    ]


def _drain(stream: IO[str]) -> None:
    while stream.read(65_536):
        pass


def _read_lines(stream: IO[str], output: queue.Queue[object]) -> None:
    for line in stream:
        output.put(line)
    output.put(_STREAM_END)


def _send_message(stream: IO[str], payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def _server_request_response(message: dict[str, Any], state: _AppServerState) -> dict[str, Any]:
    request_id = message.get("id")
    method = message.get("method")
    if method == "mcpServer/elicitation/request":
        approved = state.approve_memory_elicitation(message.get("params"))
        if not approved:
            state.summary.failed = True
        return {
            "id": request_id,
            "result": {
                "action": "accept" if approved else "decline",
                "content": {} if approved else None,
            },
        }
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        state.summary.failed = True
        return {"id": request_id, "result": {"decision": "decline"}}
    state.summary.failed = True
    return {
        "id": request_id,
        "error": {"code": -32601, "message": "unsupported benchmark client request"},
    }


def _next_message(
    messages: queue.Queue[object],
    process: subprocess.Popen[str],
    deadline: float,
) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex app-server timed out")
    try:
        line = messages.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError("Codex app-server timed out") from exc
    if line is _STREAM_END:
        raise RuntimeError(f"Codex app-server exited before completion ({process.poll()})")
    if not isinstance(line, str) or len(line) > 1_000_000:
        raise RuntimeError("Codex app-server emitted an invalid JSONL frame")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex app-server emitted invalid JSON") from exc
    if not isinstance(message, dict):
        raise RuntimeError("Codex app-server emitted a non-object message")
    return message


def _await_response(
    request_id: int,
    messages: queue.Queue[object],
    process: subprocess.Popen[str],
    stdin: IO[str],
    state: _AppServerState,
    deadline: float,
) -> dict[str, Any]:
    while True:
        message = _next_message(messages, process, deadline)
        if "method" in message and "id" in message:
            _send_message(stdin, _server_request_response(message, state))
            continue
        if "method" in message:
            state.consume_notification(message)
            continue
        if message.get("id") != request_id:
            raise RuntimeError("Codex app-server returned an unexpected response id")
        if "error" in message:
            raise RuntimeError("Codex app-server rejected a benchmark request")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex app-server returned an invalid result")
        return result


def _run_codex(arguments: argparse.Namespace) -> CodexEventSummary:
    workspace = arguments.workspace.resolve(strict=True)
    prompt_file = arguments.prompt.resolve(strict=True)
    mcp_config = arguments.mcp_config.resolve(strict=True)
    if not workspace.is_dir() or not (workspace / ".git").is_dir():
        raise ValueError("workspace must be an isolated Git checkout")
    _reject_project_codex_directory(workspace)
    for path, label in [(prompt_file, "prompt"), (mcp_config, "MCP configuration")]:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"benchmark {label} must be a regular file")
    if prompt_file.stat().st_size > 1_000_000:
        raise ValueError("benchmark prompt exceeds 1 MiB")
    prompt = prompt_file.read_text(encoding="utf-8")
    expected_memory_arguments = _expected_memory_arguments(prompt)
    _prepare_codex_home(arguments.auth_file)
    environment = os.environ.copy()
    environment.update({"CODEX_HOME": str(_CODEX_HOME), "HOME": str(_CODEX_HOME.parent)})
    process = subprocess.Popen(  # noqa: S603 - fixed executable and validated argv
        _app_server_command(arguments, mcp_config),
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_thread = threading.Thread(target=_drain, args=(process.stderr,), daemon=True)
    stdout_messages: queue.Queue[object] = queue.Queue(maxsize=1024)
    stdout_thread = threading.Thread(
        target=_read_lines,
        args=(process.stdout, stdout_messages),
        daemon=True,
    )
    stderr_thread.start()
    stdout_thread.start()
    state = _AppServerState(expected_memory_arguments=expected_memory_arguments)
    deadline = time.monotonic() + arguments.timeout_seconds
    try:
        _send_message(
            process.stdin,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "capabilities": {"experimentalApi": True},
                    "clientInfo": {
                        "name": "memoryos_real_workload",
                        "title": "MemoryOS Real Workload",
                        "version": "1.0.0",
                    },
                },
            },
        )
        _await_response(1, stdout_messages, process, process.stdin, state, deadline)
        _send_message(process.stdin, {"method": "initialized"})
        _send_message(
            process.stdin,
            {
                "method": "thread/start",
                "id": 2,
                "params": {
                    "approvalPolicy": _GRANULAR_APPROVAL_POLICY,
                    "cwd": str(workspace),
                    "ephemeral": True,
                    "model": arguments.model,
                    "sandbox": "workspace-write",
                    "serviceTier": arguments.service_tier,
                },
            },
        )
        thread_result = _await_response(2, stdout_messages, process, process.stdin, state, deadline)
        thread = thread_result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise RuntimeError("Codex app-server returned no thread id")
        _send_message(
            process.stdin,
            {
                "method": "turn/start",
                "id": 3,
                "params": {
                    "approvalPolicy": _GRANULAR_APPROVAL_POLICY,
                    "effort": arguments.reasoning_effort,
                    "input": [{"type": "text", "text": prompt}],
                    "model": arguments.model,
                    "serviceTier": arguments.service_tier,
                    "threadId": thread_id,
                },
            },
        )
        turn_result = _await_response(3, stdout_messages, process, process.stdin, state, deadline)
        turn = turn_result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise RuntimeError("Codex app-server returned no turn id")
        while not state.summary.turn_completed:
            message = _next_message(stdout_messages, process, deadline)
            if "method" in message and "id" in message:
                _send_message(process.stdin, _server_request_response(message, state))
            elif "method" in message:
                params = message.get("params")
                if (
                    message.get("method") == "turn/completed"
                    and isinstance(params, dict)
                    and (
                        params.get("threadId") != thread_id
                        or params.get("turn", {}).get("id") != turn_id
                    )
                ):
                    raise RuntimeError("Codex app-server completed an unexpected turn")
                state.consume_notification(message)
    finally:
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
    if process.returncode not in {0, -15} or not state.summary.turn_completed:
        state.summary.failed = True
    if state.summary.input_tokens is None or state.summary.output_tokens is None:
        state.summary.failed = True
    if state.protocol_error:
        agent_message = state.summary.message or ""
        state.summary.message = _bounded_message(
            f"adapter_error:{state.protocol_error}; agent_message:{agent_message}"
        )
    return state.summary


def _write_result(path: Path, summary: CodexEventSummary) -> None:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("agent result destination must be a pre-created regular file")
    payload = {
        "status": "failed" if summary.failed else "completed",
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "cost_usd": None,
        "tool_calls": summary.tool_calls,
        "message": summary.message,
    }
    resolved.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--mcp-config", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
        default="high",
    )
    parser.add_argument("--service-tier", choices=["default", "priority"], default="default")
    parser.add_argument("--timeout-seconds", type=int, choices=range(1, 1741), default=1740)
    arguments = parser.parse_args()
    try:
        summary = _run_codex(arguments)
    except Exception as exc:
        summary = CodexEventSummary(failed=True, message=f"adapter_error:{type(exc).__name__}")
    _write_result(arguments.result, summary)


if __name__ == "__main__":
    main()


__all__ = ["CodexEventSummary", "main"]
