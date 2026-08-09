from __future__ import annotations

from pathlib import Path

import pytest

from memoryos.retrieval_v2.vector import ExactVectorIndex, OptionalSqliteAnnIndex


@pytest.mark.v2
def test_exact_vector_index_remains_the_portable_baseline() -> None:
    index = ExactVectorIndex({"a": [1.0, 0.0], "b": [0.0, 1.0]})

    assert index.search([0.9, 0.1], limit=1)[0][0] == "a"


@pytest.mark.v2
def test_optional_sqlite_ann_index_and_unavailable_fallback(tmp_path: Path) -> None:
    disabled = OptionalSqliteAnnIndex(tmp_path / "disabled.db", 3, enabled=False)
    assert disabled.available is False
    assert disabled.search([1.0, 0.0, 0.0], limit=5) == []

    index = OptionalSqliteAnnIndex(tmp_path / "vectors.db", 3)
    if not index.available:
        pytest.skip(f"sqlite-vec is not installed: {index.unavailable_reason}")
    assert index.upsert({"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0]}) == 2
    results = index.search([0.9, 0.1, 0.0], limit=2)
    index.close()

    assert [item_id for item_id, _score in results] == ["a", "b"]
    assert results[0][1] > results[1][1]
