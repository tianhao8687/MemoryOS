from __future__ import annotations

import json
import math
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic import ValidationError as PydanticValidationError

from memoryos.claims.canonicalize import validate_claim_candidates
from memoryos.domain.schemas import ClaimCandidate, ProviderCandidate
from memoryos.errors import ProviderError
from memoryos.providers.base import ProviderMetadata, ProviderStats


class _ExtractorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ProviderCandidate] = Field(default_factory=list, max_length=20)
    abstain: bool = False
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_abstention(self) -> _ExtractorEnvelope:
        if self.abstain and self.candidates:
            raise ValueError("abstaining responses may not include candidates")
        return self


class _RelationshipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship: Literal[
        "equivalent", "supports", "contradicts", "independent", "supersedes_candidate", "uncertain"
    ]
    confidence: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=2000)
    abstain: bool = False


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str = Field(min_length=1, max_length=36)
    score: float = Field(ge=0, le=1)


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: list[_RerankItem] = Field(max_length=40)


class _ConsolidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["candidate", "contested", "abstain"]
    proposal: str | None = Field(default=None, max_length=5000)
    supporting_memory_ids: list[str] = Field(default_factory=list, max_length=100)
    counterevidence_memory_ids: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=0, ge=0, le=1)


class _StalenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["fresh", "suspect", "stale", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=2000)


class _OpenAIJSONProvider:
    capabilities: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 20,
        max_input_chars: int = 12000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_input_chars = max_input_chars
        self.stats = ProviderStats()

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider="openai-compatible",
            model=self.model,
            real_model=True,
            max_input_chars=self.max_input_chars,
            capabilities=self.capabilities,
        )

    def _json(self, *, system: str, user: str) -> dict[str, Any]:
        self.stats.calls += 1
        clipped = user[: self.max_input_chars]
        self.stats.input_chars += len(clipped)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": clipped},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            content = data["choices"][0]["message"]["content"]
            decoded = json.loads(content)
            if not isinstance(decoded, dict):
                raise TypeError("provider JSON root must be an object")
            return decoded
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self.stats.failures += 1
            raise ProviderError("provider returned an invalid or unavailable response") from exc


class OpenAICompatibleExtractor(_OpenAIJSONProvider):
    capabilities = ("candidate_extraction", "claim_extraction", "abstain")

    def extract(self, text: str) -> list[ProviderCandidate]:
        decoded = self._json(
            system=(
                "Extract only facts explicitly supported by the supplied coding conversation. "
                "Return JSON {candidates, abstain, reason}. Every candidate must include memory "
                "fields and claim_candidates. Every claim requires subject_hint, predicate, "
                "object_kind/object value, modality, confidence, and evidence_span with exact "
                "start/end/quote offsets into the user text. Never invent an entity or fact. "
                "Use abstain=true when evidence is insufficient. Never activate memory."
            ),
            user=text,
        )
        try:
            envelope = _ExtractorEnvelope.model_validate(decoded)
        except PydanticValidationError as exc:
            self.stats.failures += 1
            raise ProviderError("extractor response failed schema validation") from exc
        validated = []
        for candidate in envelope.candidates:
            valid, rejected = validate_claim_candidates(candidate.claim_candidates, text)
            if rejected or not valid:
                continue
            candidate.claim_candidates = valid
            validated.append(candidate)
        if envelope.candidates and not validated and not envelope.abstain:
            raise ProviderError("extractor claims were not grounded in the supplied evidence")
        return validated

    def extract_claims(self, text: str) -> list[ClaimCandidate]:
        return [claim for candidate in self.extract(text) for claim in candidate.claim_candidates]


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 20,
        max_input_chars: int = 12000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_input_chars = max_input_chars
        self.stats = ProviderStats()

    @property
    def name(self) -> str:
        return "openai-compatible"

    @property
    def model(self) -> str:
        return self._model

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider=self.name,
            model=self.model,
            real_model=True,
            max_input_chars=self.max_input_chars,
            capabilities=("embedding", "query_instruction", "document_instruction"),
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed([f"Represent this coding task for retrieval: {text}"])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed([f"Represent this coding memory for retrieval: {text}" for text in texts])

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.stats.calls += 1
        clipped = [text[: self.max_input_chars] for text in texts]
        self.stats.input_chars += sum(len(text) for text in clipped)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": clipped},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != len(clipped):
                raise ValueError("embedding response count does not match the request")
            ordered = sorted(data, key=lambda item: int(item["index"]))
            if [int(item["index"]) for item in ordered] != list(range(len(clipped))):
                raise ValueError("embedding response indexes are incomplete or duplicated")
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
            dimensions = {len(vector) for vector in vectors}
            if (
                not vectors
                or dimensions == {0}
                or len(dimensions) != 1
                or next(iter(dimensions)) > 65_536
            ):
                raise ValueError("embedding response vectors have invalid dimensions")
            if not all(math.isfinite(value) for vector in vectors for value in vector):
                raise ValueError("embedding response contains non-finite values")
            return vectors
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            self.stats.failures += 1
            raise ProviderError("embedding provider failed; FTS5 remains available") from exc


class OpenAICompatibleRelationshipJudge(_OpenAIJSONProvider):
    capabilities = ("relationship_judgement", "abstain")

    def judge(
        self, left: dict[str, Any], right: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]:
        decoded = self._json(
            system=(
                "Classify one claim pair as equivalent, supports, contradicts, independent, "
                "supersedes_candidate, or uncertain. Return JSON with relationship, confidence, "
                "explanation, abstain. Judge only from supplied claims and minimal evidence."
            ),
            user=json.dumps(
                {"left": left, "right": right, "evidence": evidence}, ensure_ascii=False
            ),
        )
        try:
            return _RelationshipResponse.model_validate(decoded).model_dump(mode="json")
        except PydanticValidationError as exc:
            self.stats.failures += 1
            raise ProviderError("relationship response failed schema validation") from exc


class OpenAICompatibleReranker(_OpenAIJSONProvider):
    capabilities = ("rerank",)

    @property
    def name(self) -> str:
        return f"openai-compatible:{self.model}"

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
        bounded = [
            {
                "memory_id": item["memory"]["id"],
                "title": item["memory"]["title"],
                "content": item["memory"]["content"][:2000],
            }
            for item in candidates[:40]
        ]
        decoded = self._json(
            system=(
                "Score each supplied coding-memory candidate from 0 to 1 for relevance to the "
                "query. Return JSON {scores:[{memory_id,score}]}. Do not add candidates."
            ),
            user=json.dumps({"query": query, "candidates": bounded}, ensure_ascii=False),
        )
        try:
            response = _RerankResponse.model_validate(decoded)
        except PydanticValidationError as exc:
            self.stats.failures += 1
            raise ProviderError("reranker response failed schema validation") from exc
        allowed = {str(item["memory_id"]) for item in bounded}
        return {item.memory_id: item.score for item in response.scores if item.memory_id in allowed}


class OpenAICompatibleConsolidationJudge(_OpenAIJSONProvider):
    capabilities = ("consolidation_judgement", "counterevidence", "abstain")

    def judge(self, episodes: list[dict[str, Any]]) -> dict[str, Any]:
        decoded = self._json(
            system=(
                "Propose a stable coding-memory abstraction only when repeated episodes support "
                "it. Return candidate, contested, or abstain and list counterevidence memory ids. "
                "Never activate or delete memory."
            ),
            user=json.dumps({"episodes": episodes[:100]}, ensure_ascii=False),
        )
        try:
            return _ConsolidationResponse.model_validate(decoded).model_dump(mode="json")
        except PydanticValidationError as exc:
            self.stats.failures += 1
            raise ProviderError("consolidation response failed schema validation") from exc


class OpenAICompatibleStalenessJudge(_OpenAIJSONProvider):
    capabilities = ("staleness_judgement", "abstain")

    def judge(self, old_evidence: str, current_summary: str) -> dict[str, Any]:
        decoded = self._json(
            system=(
                "Compare old bounded code evidence with the current bounded symbol summary. "
                "Return fresh, suspect, stale, or uncertain with confidence and explanation. "
                "This is only a judgement candidate; never mutate truth."
            ),
            user=json.dumps(
                {"old_evidence": old_evidence, "current_summary": current_summary},
                ensure_ascii=False,
            ),
        )
        try:
            return _StalenessResponse.model_validate(decoded).model_dump(mode="json")
        except PydanticValidationError as exc:
            self.stats.failures += 1
            raise ProviderError("staleness response failed schema validation") from exc


def validate_provider_candidates(value: Any) -> list[ProviderCandidate]:
    """Compatibility helper for integrations that already decoded an envelope."""

    return TypeAdapter(list[ProviderCandidate]).validate_python(value)
