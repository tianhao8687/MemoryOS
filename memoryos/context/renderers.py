from __future__ import annotations

from copy import deepcopy
from typing import Any

from memoryos.context.atoms import ContextAtom
from memoryos.context.token_meter import TokenCounter
from memoryos.domain.schemas import MemoryOperationTokenAttribution
from memoryos.retrieval.context import SECTION_ORDER

MSC_SCHEMA_VERSION = "2.3"
PAYLOAD_ACCOUNTING_MAX_ROUNDS = 32


def render_full(atoms: list[ContextAtom]) -> str:
    return render_full_items([atom.snapshot_item(index) for index, atom in enumerate(atoms)])


def render_full_items(items: list[dict[str, Any]]) -> str:
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTION_ORDER}
    for item in sorted(items, key=lambda value: int(value.get("ordinal", 0))):
        section = str(item.get("section", "CURRENT BRANCH / TASK STATE"))
        sections.setdefault(section, []).append(item)
    lines = ["Project Memory Context v2.3"]
    for name in SECTION_ORDER:
        entries = sections.get(name, [])
        if not entries:
            continue
        lines.extend(("", name))
        lines.extend(str(item["rendered_text"]) for item in entries)
    return "\n".join(lines)


def render_delta(
    *,
    added: list[ContextAtom],
    changed: list[ContextAtom],
    removed: list[dict[str, Any]],
) -> str:
    lines = ["Memory Context Delta v2.3"]
    if added:
        lines.extend(("", "ADDED"))
        lines.extend(_prefix_atom("+", atom.rendered_text) for atom in added)
    if changed:
        lines.extend(("", "CHANGED"))
        lines.extend(_prefix_atom("~", atom.rendered_text) for atom in changed)
    if removed:
        lines.extend(("", "REMOVED"))
        lines.extend(
            f"- [{item['memory_id']} @ {item['atom_sha256']}] no longer present in current context"
            for item in sorted(removed, key=lambda value: str(value["memory_id"]))
        )
    if not (added or changed or removed):
        lines.extend(("", "No memory-context changes."))
    return "\n".join(lines)


def aggregate_truth_state(atoms: list[ContextAtom]) -> str:
    states = {atom.truth_state.value for atom in atoms}
    if "contested" in states:
        return "contested"
    if "stale" in states:
        return "stale"
    if "resolved" in states:
        return "resolved"
    return "unknown"


def make_usage(
    counter: TokenCounter,
    *,
    full_context_tokens: int = 0,
    legacy_equivalent_tokens: int = 0,
    selection_latency_ms: float = 0.0,
    render_latency_ms: float = 0.0,
    evidence_expansion_tokens: int = 0,
    history_expansion_tokens: int = 0,
    other_memory_operation_llm_input_tokens: int | None = 0,
    other_memory_operation_llm_output_tokens: int | None = 0,
    other_memory_operation_token_attribution: MemoryOperationTokenAttribution = (
        MemoryOperationTokenAttribution.EXACT_ZERO
    ),
) -> dict[str, Any]:
    return {
        "counter_kind": counter.kind.value,
        "tokenizer_id": counter.tokenizer_id,
        "counter_version": counter.counter_version,
        "full_context_tokens": full_context_tokens,
        "context_text_tokens": 0,
        "payload_overhead_tokens": 0,
        "delivered_payload_tokens": 0,
        "delta_tokens": 0,
        "evidence_expansion_tokens": evidence_expansion_tokens,
        "history_expansion_tokens": history_expansion_tokens,
        "legacy_equivalent_tokens": legacy_equivalent_tokens,
        "selection_latency_ms": max(0.0, selection_latency_ms),
        "render_latency_ms": max(0.0, render_latency_ms),
        "context_compilation_llm_input_tokens": 0,
        "context_compilation_llm_output_tokens": 0,
        "other_memory_operation_llm_input_tokens": (
            max(0, other_memory_operation_llm_input_tokens)
            if other_memory_operation_llm_input_tokens is not None
            else None
        ),
        "other_memory_operation_llm_output_tokens": (
            max(0, other_memory_operation_llm_output_tokens)
            if other_memory_operation_llm_output_tokens is not None
            else None
        ),
        "other_memory_operation_token_attribution": (
            other_memory_operation_token_attribution.value
        ),
    }


def stabilize_payload_usage(payload: dict[str, Any], counter: TokenCounter) -> dict[str, Any]:
    """Reach a deterministic fixed point because the usage digits are in the payload."""

    result = deepcopy(payload)
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("MSC payload requires a usage object")
    text_tokens = counter.count_text(str(result.get("text", "")))
    usage["context_text_tokens"] = text_tokens
    for _ in range(PAYLOAD_ACCOUNTING_MAX_ROUNDS):
        delivered = counter.count_json(result)
        overhead = max(0, delivered - text_tokens)
        updates = {
            "delivered_payload_tokens": delivered,
            "payload_overhead_tokens": overhead,
            "delta_tokens": delivered if result.get("mode") == "delta" else 0,
        }
        if all(usage.get(key) == value for key, value in updates.items()):
            return result
        usage.update(updates)
    raise RuntimeError("context payload token accounting did not converge")


def _prefix_atom(prefix: str, rendered: str) -> str:
    first, *remaining = rendered.splitlines()
    normalized = first[2:] if first.startswith("- ") else first
    if not remaining:
        return f"{prefix} {normalized}"
    return "\n".join((f"{prefix} {normalized}", *remaining))


__all__ = [
    "MSC_SCHEMA_VERSION",
    "PAYLOAD_ACCOUNTING_MAX_ROUNDS",
    "aggregate_truth_state",
    "make_usage",
    "render_delta",
    "render_full",
    "render_full_items",
    "stabilize_payload_usage",
]
