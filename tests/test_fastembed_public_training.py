from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from memoryos.evaluation import fastembed_public_training as subject
from memoryos.evaluation.calibration_models import CalibrationSplit
from memoryos.evaluation.public_bootstrap_training import (
    PublicRelevanceCandidate,
    PublicRelevanceDataset,
    PublicRelevanceQuery,
)


class _FakeTextEmbedding:
    passage_calls = 0
    query_calls = 0

    @classmethod
    def list_supported_models(cls) -> list[dict[str, object]]:
        return [
            {
                "model": "example/tiny",
                "sources": {"hf": "example/tiny-onnx"},
                "dim": 2,
            }
        ]

    def __init__(self, **_: object) -> None:
        pass

    def passage_embed(self, texts: list[str], **_: object) -> list[np.ndarray]:
        type(self).passage_calls += 1
        return [_vector(text) for text in texts]

    def query_embed(self, texts: list[str], **_: object) -> list[np.ndarray]:
        type(self).query_calls += 1
        return [_vector(text) for text in texts]


def _vector(text: str) -> np.ndarray:
    return np.asarray([1.0, 0.0] if "alpha" in text else [0.0, 1.0], dtype=np.float32)


def _dataset() -> PublicRelevanceDataset:
    candidates: list[PublicRelevanceCandidate] = []
    queries: dict[CalibrationSplit, tuple[PublicRelevanceQuery, ...]] = {}
    for index, split in enumerate(CalibrationSplit):
        repository = f"example/repo-{index}"
        first = f"c-{index}-alpha"
        second = f"c-{index}-beta"
        candidates.extend(
            (
                PublicRelevanceCandidate(first, repository, "alpha implementation"),
                PublicRelevanceCandidate(second, repository, "beta documentation"),
            )
        )
        queries[split] = (
            PublicRelevanceQuery(
                query_id=f"q-{index}",
                repository_id=repository,
                split=split,
                query="alpha repair",
                candidate_ids=(first, second),
            ),
        )
    return PublicRelevanceDataset(
        dataset_id="fastembed-test",
        dataset_sha256="a" * 64,
        source_adapter_sha256="b" * 64,
        candidates=tuple(candidates),
        queries=queries,
        judgments={split: () for split in CalibrationSplit},
        limitations=(),
    )


def test_fastembed_features_are_model_bound_and_reuse_the_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    model_cache = tmp_path / "models"
    revision = "c" * 40
    repository_cache = model_cache / "models--example--tiny-onnx"
    (repository_cache / "refs").mkdir(parents=True)
    (repository_cache / "refs" / "main").write_text(revision, encoding="utf-8")
    snapshot = repository_cache / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.onnx").write_bytes(b"pinned model")
    monkeypatch.setattr(
        subject.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(TextEmbedding=_FakeTextEmbedding) if name == "fastembed" else None
        ),
    )
    monkeypatch.setattr(subject.importlib.metadata, "version", lambda _: "test-version")
    _FakeTextEmbedding.passage_calls = 0
    _FakeTextEmbedding.query_calls = 0
    cache = tmp_path / "features.npz"

    first = subject.build_fastembed_feature_bundle(
        _dataset(),
        model_cache_dir=model_cache,
        embedding_cache_path=cache,
        model_name="example/tiny",
    )
    second = subject.build_fastembed_feature_bundle(
        _dataset(),
        model_cache_dir=model_cache,
        embedding_cache_path=cache,
        model_name="example/tiny",
    )

    assert first == second
    assert first.provider_id == f"fastembed:example/tiny@{revision}"
    assert len(first.provider_source_sha256) == 64
    assert len(first.feature_adapter_sha256) == 64
    assert len(first.model_files_sha256) == 64
    assert len(first.embedding_cache_sha256) == 64
    assert first.dimensions == 2
    assert _FakeTextEmbedding.passage_calls == 1
    assert _FakeTextEmbedding.query_calls == 1
    alpha_rows = [row for row in first.feature_rows if row.candidate_id.endswith("alpha")]
    assert all(row.values[1] == 1.0 for row in alpha_rows)
