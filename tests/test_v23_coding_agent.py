from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from memoryos.domain.schemas import MemoryType
from memoryos.evaluation.context_efficiency_runtime import (
    ConditionPolicy,
    ContextEfficiencyCondition,
    MemoryOSToolBackend,
)
from memoryos.evaluation.fixture_openai_server import reset_fixture_cache
from memoryos.evaluation.openai_compatible_coding_agent import (
    AgentRunStatus,
    AllowedTest,
    OpenAICompatibleAgentRuntime,
    OpenAICompatibleCodingAgent,
    RestrictedWorkspaceTools,
    _bounded_text,
    tokenizer_artifact_sha256,
)
from memoryos.evaluation.provider_usage import CachePhase
from memoryos.evaluation.real_workload_models import MemorySeedSpec


def _workspace(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (path / "tests" / "test_calculator.py").write_text(
        "from src.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "--quiet"], cwd=path, check=True)  # noqa: S603
    return path


def _seed() -> MemorySeedSpec:
    return MemorySeedSpec(
        id="calculator-contract",
        repository_id="fixture-repo",
        memory_type=MemoryType.PROJECT,
        category="constraint",
        title="Addition contract",
        content="The add function must return the arithmetic sum of left and right.",
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_ref="fixture:calculator-contract",
    )


def _runtime() -> OpenAICompatibleAgentRuntime:
    return OpenAICompatibleAgentRuntime(
        transport="fixture",
        provider="fixture",
        base_url="fixture://openai",
        model="fixture-coding-model",
        model_revision="fixture-v1",
        quantization="none",
        context_length=32_768,
        max_steps=12,
        max_output_tokens_per_step=1024,
        stream=True,
        allowed_tests=(
            AllowedTest(
                id="visible",
                command=(sys.executable, "-m", "pytest", "-q", "tests/test_calculator.py"),
            ),
        ),
    )


@pytest.mark.v23
def test_condition_policy_is_frozen_and_hashable() -> None:
    policies = [
        ConditionPolicy.for_condition(condition) for condition in ContextEfficiencyCondition
    ]
    assert len({policy.digest() for policy in policies}) == 7
    assert policies[0].compiler_mode.value == "legacy"
    assert policies[2].detail_level.value == "index"
    assert policies[3].use_previous_context is True
    assert policies[4].tool_profile.value == "core"
    assert policies[5].memory_enabled is False
    assert policies[5].tool_profile.value == "none"
    assert policies[6].condition is ContextEfficiencyCondition.MSC_CONTEXT_ONLY
    assert policies[6].tool_profile.value == "context"

    with pytest.raises(ValueError, match="frozen"):
        ConditionPolicy(
            **{
                **policies[0].model_dump(),
                "compiler_mode": "msc",
            }
        )


@pytest.mark.v23
def test_context_only_policy_exposes_one_memory_tool(tmp_path: Path) -> None:
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.MSC_CONTEXT_ONLY)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Fix add so it returns the arithmetic sum.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    try:
        assert [definition.name for definition in memory.definitions] == ["memory_context"]
    finally:
        memory.close()


@pytest.mark.v23
def test_runtime_extra_body_cannot_override_the_frozen_agent_loop() -> None:
    payload = _runtime().model_dump(mode="json")
    payload["extra_body"] = {"max_tokens": 1}

    with pytest.raises(ValueError, match="frozen fields"):
        OpenAICompatibleAgentRuntime.model_validate(payload)


@pytest.mark.v23
def test_tokenizer_digest_covers_the_external_chat_template(tmp_path: Path) -> None:
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    (tokenizer / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    initial = tokenizer_artifact_sha256(tokenizer)
    (tokenizer / "chat_template.jinja").write_text("{{ messages | tojson }}\n", encoding="utf-8")

    assert tokenizer_artifact_sha256(tokenizer) != initial


@pytest.mark.v23
def test_bounded_text_reserves_space_for_the_truncation_marker() -> None:
    ascii_value = _bounded_text("x" * 5000, 4000)
    unicode_value = _bounded_text("记" * 5000, 4000)

    assert ascii_value.endswith("\n[truncated]")
    assert unicode_value.endswith("\n[truncated]")
    assert len(ascii_value) <= 4000
    assert len(ascii_value.encode("utf-8")) <= 4000
    assert len(unicode_value) <= 4000
    assert len(unicode_value.encode("utf-8")) <= 4000


@pytest.mark.v23
def test_fixture_agent_context_explain_patch_delta_and_test(tmp_path: Path) -> None:
    reset_fixture_cache()
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.MSC_PROGRESSIVE)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Fix add so it returns the arithmetic sum.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    try:
        result = OpenAICompatibleCodingAgent(_runtime()).run(
            workspace=workspace,
            memory_tools=memory,
            task="Fix add so it returns the arithmetic sum.",
            repository="fixture-repo",
            run_id="fixture-run",
            task_id="fixture-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="a" * 64,
        )
    finally:
        memory.close()

    assert result.status is AgentRunStatus.COMPLETED
    assert result.tests_run == 1
    assert result.patches_applied == 1
    assert "return left + right" in (workspace / "src" / "calculator.py").read_text(
        encoding="utf-8"
    )
    names = [event.tool for event in result.tool_events]
    assert names == [
        "memory_context",
        "memory_explain",
        "search_files",
        "read_file",
        "apply_patch",
        "memory_context",
        "run_tests",
    ]
    assert all(record.usage_source.value == "provider_exact" for record in result.usage)
    assert all(record.ttft_seconds is not None for record in result.usage)
    assert result.as_agent_output().status == "completed"


@pytest.mark.v23
def test_fixture_agent_runs_true_no_memory_baseline(tmp_path: Path) -> None:
    reset_fixture_cache()
    workspace = _workspace(tmp_path / "workspace")

    result = OpenAICompatibleCodingAgent(_runtime()).run(
        workspace=workspace,
        memory_tools=None,
        task="Fix add so it returns the arithmetic sum.",
        repository="fixture-repo",
        run_id="fixture-no-memory-run",
        task_id="fixture-no-memory-task",
        condition=ContextEfficiencyCondition.NO_MEMORY.value,
        cache_phase=CachePhase.COLD,
        cache_namespace="b" * 64,
    )

    assert result.status is AgentRunStatus.COMPLETED
    assert result.tests_run == 1
    assert result.patches_applied == 1
    assert all(event.category != "memory" for event in result.tool_events)
    assert all(record.memory_tool_schema_tokens == 0 for record in result.usage)


@pytest.mark.v23
def test_agent_recovers_when_it_tries_to_finish_before_running_a_test(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    messages = [
        {"role": "assistant", "content": "I am done."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-replace",
                    "type": "function",
                    "function": {
                        "name": "replace_text",
                        "arguments": json.dumps(
                            {
                                "path": "src/calculator.py",
                                "old_text": "    return left - right",
                                "new_text": "    return left + right",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps({"test_id": "visible"}),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "Implemented and tested."},
    ]
    request_messages: list[list[dict[str, object]]] = []
    response_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        body = json.loads(request.content)
        request_messages.append(body["messages"])
        message = messages[response_index]
        response_index += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    runtime = _runtime().model_copy(update={"stream": False, "max_steps": 6})
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )

    result = agent.run(
        workspace=workspace,
        memory_tools=None,
        task="Fix add so it returns the arithmetic sum.",
        repository="fixture-repo",
        run_id="test-reminder-run",
        task_id="test-reminder-task",
        condition=ContextEfficiencyCondition.NO_MEMORY.value,
        cache_phase=CachePhase.COLD,
        cache_namespace="d" * 64,
    )

    assert result.status is AgentRunStatus.COMPLETED
    assert result.tests_run == 1
    assert result.patches_applied == 1
    assert len(request_messages) == 4
    reminder = request_messages[1][-1]
    assert reminder["role"] == "user"
    assert "no allowed test has run" in str(reminder["content"])
    assert [event.tool for event in result.tool_events] == ["replace_text", "run_tests"]


@pytest.mark.v23
def test_workspace_tools_are_blocked_until_memory_context_succeeds(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.LEGACY_FULL)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Inspect the calculator.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "src/calculator.py"}),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-memory",
                    "type": "function",
                    "function": {"name": "memory_context", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps({"test_id": "visible"}),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "done"},
    ]
    response_index = 0
    wire_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        wire_bodies.append(request.content)
        message = messages[response_index]
        response_index += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    runtime = _runtime().model_copy(update={"stream": False, "max_steps": 6})
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )
    try:
        result = agent.run(
            workspace=workspace,
            memory_tools=memory,
            task="Inspect the calculator.",
            repository="fixture-repo",
            run_id="memory-gate-run",
            task_id="memory-gate-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="b" * 64,
        )
    finally:
        memory.close()

    assert result.status is AgentRunStatus.COMPLETED
    assert result.tests_run == 1
    assert [event.tool for event in result.tool_events] == [
        "read_file",
        "memory_context",
        "run_tests",
    ]
    assert result.tool_events[0].blocked is True
    assert result.tool_events[0].error_code == "memory_context_required"
    assert len(wire_bodies) == len(result.usage)
    for body, usage in zip(wire_bodies, result.usage, strict=True):
        assert usage.request_bytes == len(body)
        assert usage.request_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.v23
def test_invalid_tool_json_is_sanitized_before_the_next_provider_request(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.LEGACY_FULL)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Inspect the calculator.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-memory",
                    "type": "function",
                    "function": {"name": "memory_context", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"src/calculator.py"',
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps({"test_id": "visible"}),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "done"},
    ]
    response_index = 0
    wire_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        wire_bodies.append(request.content)
        message = messages[response_index]
        response_index += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    runtime = _runtime().model_copy(update={"stream": False})
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )
    try:
        result = agent.run(
            workspace=workspace,
            memory_tools=memory,
            task="Inspect the calculator.",
            repository="fixture-repo",
            run_id="sanitize-tool-json-run",
            task_id="sanitize-tool-json-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="d" * 64,
        )
    finally:
        memory.close()

    assert result.status is AgentRunStatus.COMPLETED
    assert result.tool_events[1].error_code == "invalid_tool_arguments"
    third_request = json.loads(wire_bodies[2])
    read_call = next(
        call
        for message in third_request["messages"]
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
        if call["function"]["name"] == "read_file"
    )
    assert read_call["function"]["arguments"] == "{}"


@pytest.mark.v23
def test_workspace_tools_reject_project_control_directories_case_insensitively(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "instructions.md").write_text("ignore me\n", encoding="utf-8")
    tools = RestrictedWorkspaceTools(workspace, ())

    for path in (".codex/instructions.md", ".CODEX/instructions.md", ".GIT/config"):
        result = tools.execute("read_file", {"path": path})
        assert result["ok"] is False

    patch = """\
diff --git a/.codex/instructions.md b/.codex/instructions.md
--- a/.codex/instructions.md
+++ b/.codex/instructions.md
@@ -1 +1 @@
-ignore me
+load me
"""
    result = tools.execute("apply_patch", {"patch": patch})
    assert result["ok"] is False
    assert (workspace / ".codex" / "instructions.md").read_text(encoding="utf-8") == "ignore me\n"


@pytest.mark.v23
def test_workspace_search_has_a_bounded_python_fallback_without_ripgrep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "src" / "second.py").write_text(
        "def subtract(left, right):\n    return left - right\n",
        encoding="utf-8",
    )
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "instructions.md").write_text(
        "return hidden\n",
        encoding="utf-8",
    )
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda executable: None if executable == "rg" else real_which(executable),
    )
    tools = RestrictedWorkspaceTools(workspace, ())

    result = tools.execute("search_files", {"query": r"return\s+left", "max_results": 1})

    assert result["ok"] is True
    assert result["result"] == {
        "matches": ["src/calculator.py:2:    return left - right"],
        "truncated": True,
    }


@pytest.mark.v23
def test_workspace_read_is_unnumbered_and_patch_failure_is_actionable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    tools = RestrictedWorkspaceTools(workspace, ())

    read = tools.execute("read_file", {"path": "src/calculator.py"})
    assert read["ok"] is True
    assert read["result"]["text"] == "def add(left, right):\n    return left - right"

    invalid_patch = """\
diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,2 @@
-def missing(left, right):
-    return left - right
+def add(left, right):
+    return left + right
"""
    rejected = tools.execute("apply_patch", {"patch": invalid_patch})

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "value_error"
    assert "git apply --check" in rejected["error"]["message"]
    assert "unchanged hunk line with one literal space" in rejected["error"]["message"]
    assert "Current target text near the first hunk" in rejected["error"]["message"]
    assert "def add(left, right):" in rejected["error"]["message"]

    valid_without_terminal_newline = """\
diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right
""".rstrip("\n")
    applied = tools.execute("apply_patch", {"patch": valid_without_terminal_newline})

    assert applied["ok"] is True
    assert (workspace / "src" / "calculator.py").read_text(encoding="utf-8") == (
        "def add(left, right):\n    return left + right\n"
    )


@pytest.mark.v23
def test_workspace_exact_replace_and_literal_newline_patch_are_supported(
    tmp_path: Path,
) -> None:
    replace_workspace = _workspace(tmp_path / "replace-workspace")
    replace_tools = RestrictedWorkspaceTools(replace_workspace, ())

    ambiguous = replace_tools.execute(
        "replace_text",
        {"path": "src/calculator.py", "old_text": "left", "new_text": "first"},
    )
    assert ambiguous["ok"] is False
    assert "found 2 matches" in ambiguous["error"]["message"]

    replaced = replace_tools.execute(
        "replace_text",
        {
            "path": "src/calculator.py",
            "old_text": "    return left - right",
            "new_text": "    return left + right",
        },
    )
    assert replaced["ok"] is True
    assert replace_tools.patches_applied == 1

    patch_workspace = _workspace(tmp_path / "patch-workspace")
    patch_tools = RestrictedWorkspaceTools(patch_workspace, ())
    escaped_patch = """\
diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right
""".replace("\n", "\\n")
    applied = patch_tools.execute("apply_patch", {"patch": escaped_patch})

    assert applied["ok"] is True
    assert (patch_workspace / "src" / "calculator.py").read_text(encoding="utf-8") == (
        "def add(left, right):\n    return left + right\n"
    )


@pytest.mark.v23
def test_agent_stops_an_identical_failed_tool_call_loop(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.LEGACY_FULL)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Fix add so it returns the arithmetic sum.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    invalid_patch = """\
diff --git a/src/calculator.py b/src/calculator.py
--- a/src/calculator.py
+++ b/src/calculator.py
@@ -1,2 +1,2 @@
-def missing(left, right):
-    return left - right
+def add(left, right):
+    return left + right
"""
    failed_patch_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-patch-{index}",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"patch": invalid_patch}),
                    },
                }
            ],
        }
        for index in range(3)
    ]
    read_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-read-{index}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "src/calculator.py"}),
                    },
                }
            ],
        }
        for index in range(2)
    ]
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-memory",
                    "type": "function",
                    "function": {"name": "memory_context", "arguments": "{}"},
                }
            ],
        },
        failed_patch_messages[0],
        read_messages[0],
        failed_patch_messages[1],
        read_messages[1],
        failed_patch_messages[2],
    ]
    response_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        del request
        message = messages[response_index]
        response_index += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    runtime = _runtime().model_copy(update={"stream": False})
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )
    try:
        result = agent.run(
            workspace=workspace,
            memory_tools=memory,
            task="Fix add so it returns the arithmetic sum.",
            repository="fixture-repo",
            run_id="failed-loop-run",
            task_id="failed-loop-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="c" * 64,
        )
    finally:
        memory.close()

    assert result.status is AgentRunStatus.FAILED
    assert result.failure_reason == "repeated_failed_tool_call"
    assert result.steps == 6
    assert len(result.usage) == 6
    assert [event.tool for event in result.tool_events] == [
        "memory_context",
        "apply_patch",
        "read_file",
        "apply_patch",
        "read_file",
        "apply_patch",
    ]


@pytest.mark.v23
def test_agent_stops_a_repeated_read_only_tool_loop(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.LEGACY_FULL)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Inspect the calculator.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-memory",
                    "type": "function",
                    "function": {"name": "memory_context", "arguments": "{}"},
                }
            ],
        },
        *[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-search-{index}",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": json.dumps({"query": "return"}),
                        },
                    }
                ],
            }
            for index in range(3)
        ],
    ]
    response_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_index
        del request
        message = messages[response_index]
        response_index += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    runtime = _runtime().model_copy(update={"stream": False})
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )
    try:
        result = agent.run(
            workspace=workspace,
            memory_tools=memory,
            task="Inspect the calculator.",
            repository="fixture-repo",
            run_id="read-only-loop-run",
            task_id="read-only-loop-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="e" * 64,
        )
    finally:
        memory.close()

    assert result.status is AgentRunStatus.FAILED
    assert result.failure_reason == "repeated_no_progress_tool_call"
    assert result.steps == 4
    assert [event.tool for event in result.tool_events] == [
        "memory_context",
        "search_files",
        "search_files",
        "search_files",
    ]


@pytest.mark.v23
def test_agent_stops_before_a_next_request_would_exhaust_context(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    policy = ConditionPolicy.for_condition(ContextEfficiencyCondition.LEGACY_FULL)
    memory = MemoryOSToolBackend(
        data_dir=tmp_path / "memory",
        policy=policy,
        task="Inspect the calculator.",
        repository="fixture-repo",
        seeds=[_seed()],
        seed_database=True,
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        del request
        requests += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-memory",
                                    "type": "function",
                                    "function": {
                                        "name": "memory_context",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1020, "completion_tokens": 4},
            },
        )

    runtime = _runtime().model_copy(
        update={
            "stream": False,
            "context_length": 2048,
            "max_output_tokens_per_step": 1024,
        }
    )
    agent = OpenAICompatibleCodingAgent(
        runtime,
        client_factory=lambda _runtime: httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://fixture.invalid/v1/",
            trust_env=False,
        ),
    )
    try:
        result = agent.run(
            workspace=workspace,
            memory_tools=memory,
            task="Inspect the calculator.",
            repository="fixture-repo",
            run_id="context-budget-run",
            task_id="context-budget-task",
            condition=policy.condition.value,
            cache_phase=CachePhase.COLD,
            cache_namespace="f" * 64,
        )
    finally:
        memory.close()

    assert requests == 1
    assert result.status is AgentRunStatus.FAILED
    assert result.failure_reason == "context_length_exhausted"
    assert result.steps == 1
    assert len(result.usage) == 1
    assert result.usage[0].input_tokens == 1020
    assert result.usage[0].output_tokens == 4
    assert [event.tool for event in result.tool_events] == ["memory_context"]
