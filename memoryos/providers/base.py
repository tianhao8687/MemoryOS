from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from memoryos.domain.schemas import ClaimCandidate, ProviderCandidate


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    model: str
    real_model: bool
    max_input_chars: int
    capabilities: tuple[str, ...]


@dataclass
class ProviderStats:
    calls: int = 0
    failures: int = 0
    input_chars: int = 0


class CandidateExtractor(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def extract(self, text: str) -> list[ProviderCandidate]: ...


class ClaimExtractor(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def extract_claims(self, text: str) -> list[ClaimCandidate]: ...


class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def metadata(self) -> ProviderMetadata: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class RelationshipJudge(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def judge(
        self, left: dict[str, Any], right: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


class Reranker(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def metadata(self) -> ProviderMetadata: ...

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]: ...


class ConsolidationJudge(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def judge(self, episodes: list[dict[str, Any]]) -> dict[str, Any]: ...


class StalenessJudge(Protocol):
    @property
    def metadata(self) -> ProviderMetadata: ...

    def judge(self, old_evidence: str, current_summary: str) -> dict[str, Any]: ...
