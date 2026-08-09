from __future__ import annotations

from typing import Protocol

import numpy as np


class VectorIndex(Protocol):
    @property
    def name(self) -> str: ...

    def search(self, query: list[float], *, limit: int) -> list[tuple[str, float]]: ...


class ExactVectorIndex:
    name = "exact-numpy"

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def search(self, query: list[float], *, limit: int) -> list[tuple[str, float]]:
        query_array = np.asarray(query, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_array))
        if not query_norm:
            return []
        scored = []
        for item_id, vector in self.vectors.items():
            stored = np.asarray(vector, dtype=np.float32)
            if stored.shape != query_array.shape:
                continue
            denominator = query_norm * float(np.linalg.norm(stored))
            if denominator:
                scored.append((item_id, float(np.dot(query_array, stored) / denominator)))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:limit]


__all__ = ["ExactVectorIndex", "VectorIndex"]
