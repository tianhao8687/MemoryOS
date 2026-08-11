from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from memoryos.evaluation.codex_benchmark_agent import (
    CodexEventSummary,
    _app_server_command,
    _AppServerState,
    _expected_memory_arguments,
    _mcp_overrides,
    _reject_project_codex_directory,
)


def test_codex_event_summary_extracts_usage_tools_and_final_message() -> None:
    summary = CodexEventSummary()

    summary.consume(
        {
            "method": "item/completed",
            "params": {"item": {"id": "mcp-1", "type": "mcpToolCall", "status": "completed"}},
        }
    )
    summary.consume(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Implemented the requested change.",
                }
            },
        }
    )
    summary.consume(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"tokenUsage": {"total": {"inputTokens": 1200, "outputTokens": 345}}},
        }
    )
    summary.consume(
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "completed", "items": []}},
        }
    )

    assert summary.turn_completed is True
    assert summary.failed is False
    assert summary.input_tokens == 1200
    assert summary.output_tokens == 345
    assert summary.tool_calls == 1
    assert summary.message == "Implemented the requested change."


def test_expected_memory_arguments_are_derived_from_the_fixed_prompt_shape() -> None:
    prompt = (
        "Repository scope: pallets/markupsafe\n\n"
        "Task:\nFix the deprecation warning.\n\n"
        "Mandatory benchmark tool protocol:\n- Call memory first.\n"
    )

    assert _expected_memory_arguments(prompt) == {
        "repo": "pallets/markupsafe",
        "task": "Fix the deprecation warning.",
        "budget": 6000,
    }


def test_mcp_overrides_accept_only_the_isolated_memory_sidecar(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "benchmark_memory": {
                        "transport": "streamable-http",
                        "url": "http://benchmark-memory:8000/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    overrides = _mcp_overrides(config)

    assert 'mcp_servers.benchmark_memory.url="http://benchmark-memory:8000/mcp"' in overrides
    assert "mcp_servers.benchmark_memory.required=true" in overrides
    assert 'mcp_servers.benchmark_memory.enabled_tools=["memory_context"]' in overrides
    assert 'mcp_servers.benchmark_memory.default_tools_approval_mode="prompt"' in overrides
    assert 'mcp_servers.benchmark_memory.tools.memory_context.approval_mode="prompt"' in overrides


def test_mcp_overrides_rejects_non_benchmark_endpoint(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "benchmark_memory": {
                        "transport": "streamable-http",
                        "url": "https://example.com/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="isolated memory sidecar"):
        _mcp_overrides(config)


def test_app_server_state_approves_only_the_expected_pending_memory_call() -> None:
    expected = {
        "repo": "pallets/markupsafe",
        "task": "Fix the deprecation warning.",
        "budget": 6000,
    }
    state = _AppServerState(expected_memory_arguments=expected)
    state.consume_notification(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "call-1",
                    "type": "mcpToolCall",
                    "server": "benchmark_memory",
                    "tool": "memory_context",
                    "arguments": expected,
                }
            },
        }
    )

    assert state.approve_memory_elicitation(
        {
            "serverName": "benchmark_memory",
            "mode": "form",
            "message": "Allow the tool?",
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "tool_params": expected,
            },
        }
    )
    assert not state.approve_memory_elicitation(
        {
            "serverName": "benchmark_memory",
            "mode": "form",
            "message": "Allow the tool?",
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "tool_params": {**expected, "budget": 7000},
            },
        }
    )


def test_app_server_state_allows_safe_call_but_flags_protocol_argument_mismatch() -> None:
    expected = {
        "repo": "pallets/markupsafe",
        "task": "Fix the deprecation warning.",
        "budget": 6000,
    }
    actual = {**expected, "task": "A harmless but non-exact task description."}
    state = _AppServerState(expected_memory_arguments=expected)
    state.consume_notification(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "id": "call-1",
                    "type": "mcpToolCall",
                    "server": "benchmark_memory",
                    "tool": "memory_context",
                    "arguments": actual,
                }
            },
        }
    )

    assert state.approve_memory_elicitation(
        {
            "serverName": "benchmark_memory",
            "mode": "form",
            "requestedSchema": {"type": "object", "properties": {}},
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "tool_params": actual,
            },
        }
    )
    assert state.protocol_error == "memory_arguments_mismatch"
    assert state.summary.failed is True


def test_app_server_command_uses_strict_config_and_keeps_workspace_sandbox(
    tmp_path: Path,
) -> None:
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers":{}}\n', encoding="utf-8")
    arguments = argparse.Namespace(
        model="gpt-5.6-sol",
        reasoning_effort="max",
        service_tier="priority",
        workspace=tmp_path,
    )

    command = _app_server_command(arguments, mcp)

    assert command[1:4] == ["app-server", "--stdio", "--strict-config"]
    assert 'sandbox_mode="workspace-write"' in command
    assert 'model="gpt-5.6-sol"' in command
    assert "agents.enabled=false" in command
    assert any(value.startswith("approval_policy={granular=") for value in command)


def test_project_codex_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()

    with pytest.raises(ValueError, match=r"project-local \.codex"):
        _reject_project_codex_directory(tmp_path)


def test_missing_project_codex_directory_is_allowed(tmp_path: Path) -> None:
    _reject_project_codex_directory(tmp_path)
