from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import math
import sys
import threading
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memoryos.evaluation.fastembed_public_training import (
    DEFAULT_FASTEMBED_MODEL,
    _canonical_hash,
    _directory_sha256,
    _resolve_model_snapshot,
    fastembed_feature_adapter_digest,
)

_QUERY_PREFIX = "Represent this coding task for retrieval: "
_DOCUMENT_PREFIX = "Represent this coding memory for retrieval: "


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    input: str | list[str]
    encoding_format: str | None = None

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str | list[str]) -> str | list[str]:
        texts = [value] if isinstance(value, str) else value
        if not texts or len(texts) > 256:
            raise ValueError("embedding input must contain between 1 and 256 texts")
        if any(not text or len(text) > 12_000 for text in texts):
            raise ValueError("embedding texts must be non-empty and at most 12000 characters")
        return value


class FastEmbedAdapter:
    """Route MemoryOS instructions to the same FastEmbed methods used during training."""

    def __init__(self, model: Any, *, dimensions: int, batch_size: int) -> None:
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        method: Any
        prepared: list[str]
        if all(text.startswith(_QUERY_PREFIX) for text in texts):
            method = self.model.query_embed
            prepared = [text.removeprefix(_QUERY_PREFIX) for text in texts]
        elif all(text.startswith(_DOCUMENT_PREFIX) for text in texts):
            method = self.model.passage_embed
            prepared = [text.removeprefix(_DOCUMENT_PREFIX) for text in texts]
        else:
            raise ValueError(
                "embedding request must contain one aligned MemoryOS query or document input type"
            )
        with self._lock:
            rows = list(method(prepared, batch_size=self.batch_size))
        vectors = [[float(value) for value in row] for row in rows]
        if len(vectors) != len(texts):
            raise ValueError("FastEmbed returned the wrong vector count")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError("FastEmbed returned an unexpected vector dimension")
        if not all(math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("FastEmbed returned a non-finite vector")
        return vectors


def create_fastembed_openai_app(
    adapter: FastEmbedAdapter,
    *,
    model_name: str,
    vector_channel_id: str,
    vector_channel_source_sha256: str,
    vector_feature_adapter_sha256: str,
) -> FastAPI:
    app = FastAPI(title="MemoryOS local FastEmbed bridge", docs_url=None, redoc_url=None)

    def health_payload() -> dict[str, Any]:
        return {
            "status": "ready",
            "provider": "fastembed",
            "model": model_name,
            "vector_channel_id": vector_channel_id,
            "vector_channel_source_sha256": vector_channel_source_sha256,
            "vector_feature_adapter_sha256": vector_feature_adapter_sha256,
            "dimensions": adapter.dimensions,
            "training_method_alignment": {
                "query": "query_embed",
                "document": "passage_embed",
            },
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return health_payload()

    @app.get("/v1/health")
    def versioned_health() -> dict[str, Any]:
        return health_payload()

    @app.post("/v1/embeddings")
    def embeddings(request: EmbeddingRequest) -> dict[str, Any]:
        if request.model != model_name:
            raise HTTPException(status_code=400, detail="requested embedding model is unavailable")
        if request.encoding_format not in {None, "float"}:
            raise HTTPException(status_code=400, detail="only float embeddings are supported")
        texts = [request.input] if isinstance(request.input, str) else request.input
        try:
            vectors = adapter.embed(texts)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="local embedding failed") from exc
        return {
            "object": "list",
            "model": model_name,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    return app


def load_fastembed_adapter(
    *,
    dependency_path: Path,
    model_cache: Path,
    model_name: str,
    threads: int,
    batch_size: int,
    expected_vector_channel_id: str,
    expected_vector_channel_source_sha256: str,
    expected_vector_feature_adapter_sha256: str,
) -> FastEmbedAdapter:
    resolved_dependencies = dependency_path.resolve(strict=True)
    resolved_cache = model_cache.resolve(strict=True)
    if str(resolved_dependencies) not in sys.path:
        sys.path.append(str(resolved_dependencies))
    fastembed: Any = importlib.import_module("fastembed")
    supported = {
        str(description["model"]): description
        for description in fastembed.TextEmbedding.list_supported_models()
    }
    description = supported.get(model_name)
    if description is None:
        raise ValueError(f"FastEmbed does not support model {model_name}")
    sources = cast(dict[str, object], description["sources"])
    hugging_face_source = sources.get("hf")
    if not isinstance(hugging_face_source, str) or not hugging_face_source:
        raise ValueError(f"FastEmbed model {model_name} has no hashable Hugging Face source")
    model: Any = fastembed.TextEmbedding(
        model_name=model_name,
        cache_dir=str(resolved_cache),
        threads=threads,
    )
    model_revision, model_snapshot = _resolve_model_snapshot(
        resolved_cache,
        hugging_face_source,
    )
    model_files_sha256 = _directory_sha256(model_snapshot)
    dimensions = int(description["dim"])
    actual_channel_id = f"fastembed:{model_name}@{model_revision}"
    actual_channel_source_sha256 = _canonical_hash(
        {
            "dimensions": dimensions,
            "fastembed_version": importlib.metadata.version("fastembed"),
            "hugging_face_source": hugging_face_source,
            "model_files_sha256": model_files_sha256,
            "model_name": model_name,
            "model_revision": model_revision,
            "query_method": "query_embed",
            "document_method": "passage_embed",
            "score_transform": "normalized_cosine_shifted_to_unit_interval",
        }
    )
    actual_adapter_sha256 = fastembed_feature_adapter_digest()
    if actual_channel_id != expected_vector_channel_id:
        raise ValueError("local FastEmbed revision does not match the expected vector channel")
    if actual_channel_source_sha256 != expected_vector_channel_source_sha256:
        raise ValueError("local FastEmbed files do not match the expected vector channel source")
    if actual_adapter_sha256 != expected_vector_feature_adapter_sha256:
        raise ValueError("local FastEmbed adapter does not match the expected training adapter")
    return FastEmbedAdapter(
        model,
        dimensions=dimensions,
        batch_size=batch_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve a local FastEmbed model through the OpenAI embeddings schema."
    )
    parser.add_argument("--dependency-path", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_FASTEMBED_MODEL)
    parser.add_argument("--vector-channel-id", required=True)
    parser.add_argument("--vector-channel-source-sha256", required=True)
    parser.add_argument("--vector-feature-adapter-sha256", required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    arguments = parser.parse_args()
    if arguments.threads < 1 or arguments.batch_size < 1:
        raise ValueError("threads and batch_size must be positive")
    adapter = load_fastembed_adapter(
        dependency_path=arguments.dependency_path,
        model_cache=arguments.model_cache,
        model_name=arguments.model,
        threads=arguments.threads,
        batch_size=arguments.batch_size,
        expected_vector_channel_id=arguments.vector_channel_id,
        expected_vector_channel_source_sha256=arguments.vector_channel_source_sha256,
        expected_vector_feature_adapter_sha256=arguments.vector_feature_adapter_sha256,
    )
    app = create_fastembed_openai_app(
        adapter,
        model_name=arguments.model,
        vector_channel_id=arguments.vector_channel_id,
        vector_channel_source_sha256=arguments.vector_channel_source_sha256,
        vector_feature_adapter_sha256=arguments.vector_feature_adapter_sha256,
    )
    uvicorn.run(app, host=arguments.host, port=arguments.port, access_log=False)


__all__ = [
    "EmbeddingRequest",
    "FastEmbedAdapter",
    "create_fastembed_openai_app",
    "load_fastembed_adapter",
]


if __name__ == "__main__":
    main()
