from __future__ import annotations

import re

from memoryos.claims.canonicalize import extract_claim_candidates
from memoryos.domain.schemas import ClaimCandidate, MemoryType, ProviderCandidate
from memoryos.providers.base import ProviderMetadata, ProviderStats
from memoryos.security.redaction import redact_secrets

SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|[\r\n]+")  # noqa: RUF001
RULES: tuple[tuple[re.Pattern[str], str, MemoryType, float], ...] = (
    (
        re.compile(r"(?i)\b(prefer|preference|always use)\b|偏好|以后"),
        "preference",
        MemoryType.PREFERENCE,
        0.78,
    ),
    (
        re.compile(r"(?i)\b(decide|decided|use|adopt|standardize)\b|决定|统一|采用"),
        "decision",
        MemoryType.PROJECT,
        0.84,
    ),
    (
        re.compile(r"(?i)\b(do not|must not|never|constraint|required|requires?)\b|不要|禁止|必须"),
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
        re.compile(r"(?i)\b(current goal|working on|next task)\b|当前目标|正在|下一步"),
        "state",
        MemoryType.WORKING,
        0.72,
    ),
)


class HeuristicExtractor:
    def __init__(self) -> None:
        self.stats = ProviderStats()

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider="heuristic",
            model="deterministic-rules-v2",
            real_model=False,
            max_input_chars=50000,
            capabilities=("candidate_extraction", "claim_extraction", "offline_fallback"),
        )

    def extract(self, text: str) -> list[ProviderCandidate]:
        self.stats.calls += 1
        self.stats.input_chars += len(text)
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
        claims = self.extract_claims(text)
        for candidate in results:
            candidate.claim_candidates = [
                claim for claim in claims if claim.evidence_span.quote in candidate.content
            ]
        return results[:20]

    def extract_claims(self, text: str) -> list[ClaimCandidate]:
        return extract_claim_candidates(text)
