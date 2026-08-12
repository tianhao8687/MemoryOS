from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from memoryos.evaluation.fastembed_openai_service import (
    FastEmbedAdapter,
    create_fastembed_openai_app,
)


class _FakeFastEmbedModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], int]] = []

    def query_embed(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        self.calls.append(("query", texts, batch_size))
        return [[0.1, 0.9] for _ in texts]

    def passage_embed(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        self.calls.append(("document", texts, batch_size))
        return [[0.8, 0.2] for _ in texts]

    def embed(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        self.calls.append(("generic", texts, batch_size))
        return [[0.5, 0.5] for _ in texts]


def _client(model: Any) -> TestClient:
    return TestClient(
        create_fastembed_openai_app(
            FastEmbedAdapter(model, dimensions=2, batch_size=32),
            model_name="BAAI/bge-small-en-v1.5",
            vector_channel_id="fastembed:BAAI/bge-small-en-v1.5@revision",
            vector_channel_source_sha256="a" * 64,
            vector_feature_adapter_sha256="b" * 64,
        )
    )


def test_bridge_preserves_training_query_and_document_methods() -> None:
    model = _FakeFastEmbedModel()
    client = _client(model)

    query = client.post(
        "/v1/embeddings",
        json={
            "model": "BAAI/bge-small-en-v1.5",
            "input": ["Represent this coding task for retrieval: repair parser"],
        },
    )
    document = client.post(
        "/v1/embeddings",
        json={
            "model": "BAAI/bge-small-en-v1.5",
            "input": ["Represent this coding memory for retrieval: parser decision"],
        },
    )

    assert query.status_code == 200
    assert document.status_code == 200
    assert model.calls == [
        ("query", ["repair parser"], 32),
        ("document", ["parser decision"], 32),
    ]
    assert query.json()["data"][0]["embedding"] == [0.1, 0.9]
    health = client.get("/v1/health").json()
    assert health["vector_channel_source_sha256"] == "a" * 64
    assert health["vector_feature_adapter_sha256"] == "b" * 64
    assert health["training_method_alignment"] == {
        "query": "query_embed",
        "document": "passage_embed",
    }


def test_bridge_rejects_unbound_models_and_encodings() -> None:
    client = _client(_FakeFastEmbedModel())
    wrong_model = client.post(
        "/v1/embeddings",
        json={"model": "other-model", "input": "query"},
    )
    wrong_encoding = client.post(
        "/v1/embeddings",
        json={
            "model": "BAAI/bge-small-en-v1.5",
            "input": "query",
            "encoding_format": "base64",
        },
    )
    unaligned = client.post(
        "/v1/embeddings",
        json={"model": "BAAI/bge-small-en-v1.5", "input": "query without instruction"},
    )
    mixed = client.post(
        "/v1/embeddings",
        json={
            "model": "BAAI/bge-small-en-v1.5",
            "input": [
                "Represent this coding task for retrieval: repair parser",
                "Represent this coding memory for retrieval: parser decision",
            ],
        },
    )
    assert wrong_model.status_code == 400
    assert wrong_encoding.status_code == 400
    assert unaligned.status_code == 500
    assert mixed.status_code == 500
