from __future__ import annotations

from typing import Protocol

from memoryos.domain.schemas import ProviderCandidate


class CandidateExtractor(Protocol):
    def extract(self, text: str) -> list[ProviderCandidate]: ...


class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
