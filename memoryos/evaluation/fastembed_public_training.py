from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np

from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import (
    PublicFeatureRow,
    PublicRelevanceDataset,
    build_public_feature_rows,
)

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(frozen=True)
class FastEmbedFeatureBundle:
    feature_rows: tuple[PublicFeatureRow, ...]
    provider_id: str
    provider_source_sha256: str
    feature_adapter_sha256: str
    model_revision: str
    model_files_sha256: str
    embedding_cache_sha256: str
    dimensions: int
    candidate_count: int
    query_count: int
    limitations: tuple[str, ...]


def build_fastembed_feature_bundle(
    dataset: PublicRelevanceDataset,
    *,
    model_cache_dir: Path,
    embedding_cache_path: Path,
    model_name: str = DEFAULT_FASTEMBED_MODEL,
    threads: int = 4,
    batch_size: int = 128,
) -> FastEmbedFeatureBundle:
    """Build FTS/BGE ranks with a SHA-bound, reusable embedding cache."""

    if threads < 1 or batch_size < 1:
        raise ValueError("FastEmbed threads and batch_size must be positive")
    if embedding_cache_path.suffix.lower() != ".npz":
        raise ValueError("FastEmbed embedding cache must use the .npz suffix")

    fastembed: Any = importlib.import_module("fastembed")
    text_embedding: Any = fastembed.TextEmbedding
    supported = {str(item["model"]): item for item in text_embedding.list_supported_models()}
    description = supported.get(model_name)
    if description is None:
        raise ValueError(f"FastEmbed does not support model {model_name}")
    sources = cast(dict[str, object], description["sources"])
    hugging_face_source = sources.get("hf")
    if not isinstance(hugging_face_source, str) or not hugging_face_source:
        raise ValueError(f"FastEmbed model {model_name} has no hashable Hugging Face source")

    resolved_model_cache = model_cache_dir.resolve()
    resolved_model_cache.mkdir(parents=True, exist_ok=True)
    model: Any = text_embedding(
        model_name=model_name,
        cache_dir=str(resolved_model_cache),
        threads=threads,
    )
    model_revision, model_snapshot = _resolve_model_snapshot(
        resolved_model_cache,
        hugging_face_source,
    )
    model_files_sha256 = _directory_sha256(model_snapshot)
    fastembed_version = importlib.metadata.version("fastembed")
    provider_id = f"fastembed:{model_name}@{model_revision}"
    provider_source_sha256 = _canonical_hash(
        {
            "dimensions": int(description["dim"]),
            "fastembed_version": fastembed_version,
            "hugging_face_source": hugging_face_source,
            "model_files_sha256": model_files_sha256,
            "model_name": model_name,
            "model_revision": model_revision,
            "query_method": "query_embed",
            "document_method": "passage_embed",
            "score_transform": "normalized_cosine_shifted_to_unit_interval",
        }
    )

    candidates = sorted(dataset.candidates, key=lambda item: item.id)
    queries = sorted(
        (query for split in CalibrationSplit for query in dataset.queries[split]),
        key=lambda item: item.query_id,
    )
    cache_identity = _canonical_json(
        {
            "candidate_ids": [candidate.id for candidate in candidates],
            "dataset_sha256": dataset.dataset_sha256,
            "provider_source_sha256": provider_source_sha256,
            "query_ids": [query.query_id for query in queries],
            "schema_version": "1.0",
        }
    )
    candidate_vectors, query_vectors = _load_or_build_embedding_cache(
        model,
        candidates=[candidate.text for candidate in candidates],
        queries=[query.query for query in queries],
        cache_identity=cache_identity,
        cache_path=embedding_cache_path.resolve(),
        batch_size=batch_size,
    )
    expected_dimensions = int(description["dim"])
    _validate_embeddings(
        candidate_vectors,
        expected_rows=len(candidates),
        expected_dimensions=expected_dimensions,
        name="candidate",
    )
    _validate_embeddings(
        query_vectors,
        expected_rows=len(queries),
        expected_dimensions=expected_dimensions,
        name="query",
    )
    candidate_vectors = _normalized(candidate_vectors)
    query_vectors = _normalized(query_vectors)

    candidate_index = {candidate.id: index for index, candidate in enumerate(candidates)}
    query_index = {query.query_id: index for index, query in enumerate(queries)}
    vector_scores_by_query: dict[str, dict[str, float]] = {}
    for query in queries:
        indices = np.asarray(
            [candidate_index[candidate_id] for candidate_id in query.candidate_ids],
            dtype=np.int64,
        )
        similarities = candidate_vectors[indices] @ query_vectors[query_index[query.query_id]]
        vector_scores_by_query[query.query_id] = {
            candidate_id: (float(similarity) + 1.0) / 2.0
            for candidate_id, similarity in zip(
                query.candidate_ids,
                similarities,
                strict=True,
            )
        }

    feature_rows = build_public_feature_rows(
        dataset,
        vector_scores_by_query=vector_scores_by_query,
    )
    embedding_cache_sha256 = _file_sha256(embedding_cache_path.resolve())
    return FastEmbedFeatureBundle(
        feature_rows=tuple(feature_rows),
        provider_id=provider_id,
        provider_source_sha256=provider_source_sha256,
        feature_adapter_sha256=fastembed_feature_adapter_digest(),
        model_revision=model_revision,
        model_files_sha256=model_files_sha256,
        embedding_cache_sha256=embedding_cache_sha256,
        dimensions=expected_dimensions,
        candidate_count=len(candidates),
        query_count=len(queries),
        limitations=(
            f"The vector channel uses the generic English retrieval model {model_name}, not a "
            "MemoryOS-specific fine-tune.",
            "The production embedding provider is currently unconfigured, so this model is a "
            "local training baseline rather than proof of production-provider behavior.",
            "Embedding inputs are truncated according to the model's 512-token limit.",
        ),
    )


def fastembed_feature_adapter_digest() -> str:
    normalized = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_or_build_embedding_cache(
    model: Any,
    *,
    candidates: list[str],
    queries: list[str],
    cache_identity: str,
    cache_path: Path,
    batch_size: int,
) -> tuple[np.ndarray, np.ndarray]:
    if cache_path.is_file():
        with np.load(cache_path, allow_pickle=False) as cached:
            identity = str(cached["identity"].item())
            if identity == cache_identity:
                return (
                    np.asarray(cached["candidate_vectors"], dtype=np.float32),
                    np.asarray(cached["query_vectors"], dtype=np.float32),
                )

    candidate_vectors = np.asarray(
        list(model.passage_embed(candidates, batch_size=batch_size)),
        dtype=np.float32,
    )
    query_vectors = np.asarray(
        list(model.query_embed(queries, batch_size=batch_size)),
        dtype=np.float32,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(".tmp.npz")
    np.savez_compressed(
        temporary,
        identity=np.asarray(cache_identity),
        candidate_vectors=candidate_vectors,
        query_vectors=query_vectors,
    )
    temporary.replace(cache_path)
    return candidate_vectors, query_vectors


def _validate_embeddings(
    values: np.ndarray,
    *,
    expected_rows: int,
    expected_dimensions: int,
    name: str,
) -> None:
    if values.shape != (expected_rows, expected_dimensions):
        raise ValueError(
            f"FastEmbed {name} matrix has shape {values.shape}, expected "
            f"({expected_rows}, {expected_dimensions})"
        )
    if not np.isfinite(values).all():
        raise ValueError(f"FastEmbed {name} matrix contains non-finite values")
    if any(math.isclose(float(norm), 0.0) for norm in np.linalg.norm(values, axis=1)):
        raise ValueError(f"FastEmbed {name} matrix contains a zero vector")


def _normalized(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return cast(np.ndarray[Any, np.dtype[np.float32]], values / norms)


def _resolve_model_snapshot(cache_dir: Path, hugging_face_source: str) -> tuple[str, Path]:
    repository_cache = cache_dir / f"models--{hugging_face_source.replace('/', '--')}"
    reference = repository_cache / "refs" / "main"
    if not reference.is_file():
        raise ValueError(f"FastEmbed model cache has no pinned main revision: {reference}")
    revision = reference.read_text(encoding="utf-8").strip()
    if not revision or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("FastEmbed model cache has an invalid revision")
    snapshot = repository_cache / "snapshots" / revision
    if not snapshot.is_dir():
        raise ValueError(f"FastEmbed model snapshot is missing: {snapshot}")
    return revision, snapshot


def _directory_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"cannot hash empty model snapshot: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEFAULT_FASTEMBED_MODEL",
    "FastEmbedFeatureBundle",
    "build_fastembed_feature_bundle",
    "fastembed_feature_adapter_digest",
]
