from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from memoryos.domain.schemas import ProviderCandidate
from memoryos.errors import ProviderError


class OpenAICompatibleExtractor:
    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def extract(self, text: str) -> list[ProviderCandidate]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return JSON with a candidates array. Each candidate must contain "
                        "title, content, "
                        "memory_type, category, confidence, and importance. Never activate memory."
                    ),
                },
                {"role": "user", "content": text[:12000]},
            ],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            content = data["choices"][0]["message"]["content"]
            decoded = json.loads(content)
            return TypeAdapter(list[ProviderCandidate]).validate_python(decoded["candidates"])
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            PydanticValidationError,
        ) as exc:
            raise ProviderError("extractor returned an invalid or unavailable response") from exc


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, *, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.api_key = api_key

    @property
    def name(self) -> str:
        return "openai-compatible"

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": texts},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: int(item["index"]))
            return [[float(value) for value in item["embedding"]] for item in ordered]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError("embedding provider failed; FTS5 remains available") from exc
