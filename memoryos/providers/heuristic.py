from __future__ import annotations

import re

from memoryos.domain.schemas import MemoryType, ProviderCandidate
from memoryos.security.redaction import redact_secrets

SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|[\r\n]+")  # noqa: RUF001
RULES: tuple[tuple[re.Pattern[str], str, MemoryType, float], ...] = (
    (
        re.compile(r"(?i)\b(decide|decided|use|adopt|standardize)\b|决定|统一|采用"),
        "decision",
        MemoryType.PROJECT,
        0.84,
    ),
    (
        re.compile(r"(?i)\b(do not|must not|never|constraint|required)\b|不要|禁止|必须"),
        "constraint",
        MemoryType.PROCEDURAL,
        0.88,
    ),
    (
        re.compile(r"(?i)\b(failed|failure|broke|root cause|did not work)\b|失败|根因|不工作"),
        "failure",
        MemoryType.EPISODIC,
        0.82,
    ),
    (
        re.compile(r"(?i)\b(prefer|preference|always use)\b|偏好|以后"),
        "preference",
        MemoryType.PREFERENCE,
        0.78,
    ),
    (
        re.compile(r"(?i)\b(current goal|working on|next task)\b|当前目标|正在|下一步"),
        "state",
        MemoryType.WORKING,
        0.72,
    ),
)


class HeuristicExtractor:
    def extract(self, text: str) -> list[ProviderCandidate]:
        results: list[ProviderCandidate] = []
        for sentence in SENTENCE_SPLIT.split(text):
            sentence = sentence.strip(" -\t")
            if len(sentence) < 8:
                continue
            for pattern, category, memory_type, confidence in RULES:
                if pattern.search(sentence):
                    cleaned = redact_secrets(sentence, max_length=2000)
                    results.append(
                        ProviderCandidate(
                            title=cleaned.text[:120].rstrip(".:。"),
                            content=cleaned.text,
                            memory_type=memory_type,
                            category=category,
                            confidence=confidence,
                            importance=0.65
                            if category in {"decision", "constraint", "failure"}
                            else 0.5,
                            ttl_seconds=604800 if memory_type is MemoryType.WORKING else None,
                        )
                    )
                    break
        return results[:20]
