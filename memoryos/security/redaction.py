from __future__ import annotations

import re
from dataclasses import dataclass

REDACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    (
        "password_assignment",
        re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|secret)\s*[:=]\s*[^\s,;]{6,}"),
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    detected_types: tuple[str, ...]

    @property
    def was_redacted(self) -> bool:
        return bool(self.detected_types)


def redact_secrets(text: str, *, max_length: int | None = None) -> RedactionResult:
    redacted = text
    detected: list[str] = []
    for name, pattern in REDACTION_RULES:
        if pattern.search(redacted):
            detected.append(name)
            redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    if max_length is not None and len(redacted) > max_length:
        redacted = f"{redacted[:max_length]}…[TRUNCATED]"
    return RedactionResult(redacted, tuple(dict.fromkeys(detected)))
