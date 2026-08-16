from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from memoryos.context.token_meter import canonical_json

_CACHE: set[tuple[str, str]] = set()
_CACHE_LOCK = threading.Lock()
_HANDLE = re.compile(r"\[([0-9a-f-]{8,})\s+@\s+([0-9a-f]{64})\]")


def create_fixture_openai_app() -> FastAPI:
    app = FastAPI(title="MemoryOS deterministic OpenAI fixture")

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "fixture-coding-model", "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        body = await request.json()
        payload = fixture_completion(body)
        if body.get("stream") is True:
            return StreamingResponse(
                _stream_payload(payload),
                media_type="text/event-stream",
            )
        return JSONResponse(payload)

    return app


def fixture_http_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path.endswith("/models"):
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "fixture-coding-model"}]},
        )
    if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
        return httpx.Response(404, json={"error": {"message": "fixture route not found"}})
    try:
        body = json.loads(request.content)
        payload = fixture_completion(body)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return httpx.Response(400, json={"error": {"message": str(exc)}})
    if body.get("stream") is True:
        return httpx.Response(
            200,
            content=b"".join(item.encode("utf-8") for item in _stream_payload(payload)),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(200, json=payload)


def fixture_completion(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    messages = body.get("messages")
    tools = body.get("tools")
    model = body.get("model")
    if not isinstance(messages, list) or not isinstance(tools, list) or not isinstance(model, str):
        raise ValueError("fixture requires model, messages, and tools")
    names = _tool_names(tools)
    calls = _executed_tools(messages)
    message = _next_message(messages, names, calls, tools)
    request_text = canonical_json(
        {
            "messages": messages,
            "model": model,
            "seed": body.get("seed"),
            "temperature": body.get("temperature"),
            "tools": tools,
        }
    )
    response_text = canonical_json(message)
    prompt_tokens = _fixture_tokens(request_text)
    completion_tokens = _fixture_tokens(response_text)
    namespace = str(body.get("user_id", "fixture-default"))
    cache_key = (namespace, hashlib.sha256(request_text.encode("utf-8")).hexdigest())
    with _CACHE_LOCK:
        hit = prompt_tokens if cache_key in _CACHE else 0
        _CACHE.add(cache_key)
    miss = prompt_tokens - hit
    request_id = hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:24]
    return {
        "id": f"fixture-{request_id}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "system_fingerprint": "memoryos-fixture-v1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def reset_fixture_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _next_message(
    messages: list[Any],
    names: set[str],
    calls: list[str],
    tools: list[Any],
) -> dict[str, Any]:
    if "memory_context" in names and calls.count("memory_context") == 0:
        return _tool_call("memory_context", {})

    latest_context = _latest_tool_result(messages, "memory_context")
    progressive = _deep_get(latest_context, "result", "experiment", "detail_level") == "index"
    if progressive and "memory_explain" in names and "memory_explain" not in calls:
        serialized = canonical_json(latest_context)
        match = _HANDLE.search(serialized)
        arguments: dict[str, Any] = {}
        if match is not None:
            # The handle proves the model selected an indexed record. The benchmark
            # asks for its current evidence without pinning a hash because pinned
            # constraints can be rendered at FACT granularity in an INDEX response.
            arguments = {"memory_id": match.group(1)}
        else:
            memory_id = _first_memory_id(latest_context)
            if memory_id is not None:
                arguments = {"memory_id": memory_id}
        if arguments:
            return _tool_call("memory_explain", arguments)

    if "search_files" not in calls:
        return _tool_call("search_files", {"query": "def add", "path": "."})
    if "read_file" not in calls:
        return _tool_call("read_file", {"path": "src/calculator.py", "start_line": 1})
    if "apply_patch" not in calls:
        patch = (
            "diff --git a/src/calculator.py b/src/calculator.py\n"
            "--- a/src/calculator.py\n"
            "+++ b/src/calculator.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(left, right):\n"
            "-    return left - right\n"
            "+    return left + right\n"
        )
        return _tool_call("apply_patch", {"patch": patch})
    if "memory_context" in names and calls.count("memory_context") == 1:
        return _tool_call("memory_context", {})
    if "run_tests" not in calls:
        test_ids = _test_ids(tools)
        if not test_ids:
            return {"role": "assistant", "content": "No frozen test is available."}
        return _tool_call("run_tests", {"test_id": test_ids[0]})
    return {
        "role": "assistant",
        "content": "Implemented the requested change and ran the frozen visible test.",
    }


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(f"{name}:{canonical_json(arguments)}".encode()).hexdigest()[:16]
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"fixture-call-{digest}",
                "type": "function",
                "function": {"name": name, "arguments": canonical_json(arguments)},
            }
        ],
    }


def _tool_names(tools: list[Any]) -> set[str]:
    return {
        str(item["function"]["name"])
        for item in tools
        if isinstance(item, dict)
        and isinstance(item.get("function"), dict)
        and isinstance(item["function"].get("name"), str)
    }


def _executed_tools(messages: list[Any]) -> list[str]:
    result: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                name = call["function"].get("name")
                if isinstance(name, str):
                    result.append(name)
    return result


def _latest_tool_result(messages: list[Any], tool_name: str) -> dict[str, Any]:
    call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            if (
                isinstance(call, dict)
                and isinstance(call.get("function"), dict)
                and call["function"].get("name") == tool_name
                and isinstance(call.get("id"), str)
            ):
                call_ids.add(call["id"])
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "tool"
            and message.get("tool_call_id") in call_ids
            and isinstance(message.get("content"), str)
        ):
            try:
                value = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _first_memory_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "memory_id" and isinstance(item, str):
                return item
            found = _first_memory_id(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_memory_id(item)
            if found is not None:
                return found
    return None


def _deep_get(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _test_ids(tools: list[Any]) -> list[str]:
    for item in tools:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or function.get("name") != "run_tests":
            continue
        parameters = function.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        test_id = properties.get("test_id") if isinstance(properties, dict) else None
        values = test_id.get("enum") if isinstance(test_id, dict) else None
        if isinstance(values, list):
            return [str(value) for value in values]
    return []


def _fixture_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _stream_payload(payload: dict[str, Any]) -> Iterator[str]:
    message = payload["choices"][0]["message"]
    delta = {key: value for key, value in message.items() if key != "role"}
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        delta["tool_calls"] = [
            {**call, "index": index} if isinstance(call, dict) else call
            for index, call in enumerate(tool_calls)
        ]
    first = {
        "id": payload["id"],
        "object": "chat.completion.chunk",
        "created": 0,
        "model": payload["model"],
        "choices": [{"index": 0, "finish_reason": None, "delta": {"role": "assistant", **delta}}],
        "usage": None,
    }
    finish = {
        "id": payload["id"],
        "object": "chat.completion.chunk",
        "created": 0,
        "model": payload["model"],
        "choices": [
            {
                "index": 0,
                "finish_reason": payload["choices"][0]["finish_reason"],
                "delta": {},
            }
        ],
        "usage": payload["usage"],
    }
    yield f"data: {canonical_json(first)}\n\n"
    yield f"data: {canonical_json(finish)}\n\n"
    yield "data: [DONE]\n\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic OpenAI fixture server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    arguments = parser.parse_args()
    uvicorn.run(create_fixture_openai_app(), host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()


__all__ = [
    "create_fixture_openai_app",
    "fixture_completion",
    "fixture_http_handler",
    "reset_fixture_cache",
]
