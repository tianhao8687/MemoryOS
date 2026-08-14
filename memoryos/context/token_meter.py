from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol

from memoryos.domain.schemas import TokenCounterKind

UTF8_BYTES_PER_ESTIMATED_TOKEN = 4


def canonical_json(value: Any) -> str:
    """Serialize values exactly once for budgeting, hashing, and schema snapshots."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        default=_canonical_json_default,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = (
            value.astimezone(UTC)
            if value.tzinfo is not None and value.utcoffset() is not None
            else value
        )
        return normalized.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class TokenCounter(Protocol):
    @property
    def tokenizer_id(self) -> str: ...

    @property
    def kind(self) -> TokenCounterKind: ...

    @property
    def counter_version(self) -> str: ...

    def count_text(self, text: str) -> int: ...

    def count_json(self, value: Any) -> int: ...


class UnicodeHeuristicTokenCounter:
    """Deterministic estimate; it deliberately makes no model-tokenizer claim."""

    tokenizer_id = "unicode-heuristic-v1"
    kind = TokenCounterKind.ESTIMATED
    counter_version = "1.0.0"

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        return max(
            1,
            math.ceil(len(text.encode("utf-8")) / UTF8_BYTES_PER_ESTIMATED_TOKEN),
        )

    def count_json(self, value: Any) -> int:
        return self.count_text(canonical_json(value))


class FunctionTokenCounter:
    """Injection adapter for a provider- or harness-owned exact tokenizer."""

    kind = TokenCounterKind.EXACT

    def __init__(
        self,
        *,
        tokenizer_id: str,
        counter_version: str,
        count: Callable[[str], int],
    ) -> None:
        if not tokenizer_id.strip() or not counter_version.strip():
            raise ValueError("exact counters require tokenizer_id and counter_version")
        self.tokenizer_id = tokenizer_id.strip()
        self.counter_version = counter_version.strip()
        self._count = count

    def count_text(self, text: str) -> int:
        value = self._count(text)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("token counter returned an invalid token count")
        return value

    def count_json(self, value: Any) -> int:
        return self.count_text(canonical_json(value))


def counter_fingerprint(counter: TokenCounter) -> str:
    payload = canonical_json(
        {
            "counter_kind": counter.kind.value,
            "counter_version": counter.counter_version,
            "tokenizer_id": counter.tokenizer_id,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "UTF8_BYTES_PER_ESTIMATED_TOKEN",
    "FunctionTokenCounter",
    "TokenCounter",
    "UnicodeHeuristicTokenCounter",
    "canonical_json",
    "counter_fingerprint",
]
