from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import select, text

from memoryos.db.models import EmbeddingRow, MemoryRow
from memoryos.db.session import Database
from memoryos.domain.schemas import MemoryStatus, SearchRequest
from memoryos.errors import ProviderError
from memoryos.providers.base import EmbeddingProvider
from memoryos.retrieval.vector import ExactVectorIndex

TOKEN_RE = re.compile(r"[\w.]+", re.UNICODE)


def _fts_query(query: str) -> str:
    tokens = TOKEN_RE.findall(query.lower())
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:24])


def _scope_weight(scope_type: str) -> float:
    return {"task": 1.0, "branch": 0.92, "repository": 0.84, "workspace": 0.72, "user": 0.62}.get(
        scope_type, 0.5
    )


class RetrievalEngine:
    def __init__(
        self, database: Database, embedding_provider: EmbeddingProvider | None = None
    ) -> None:
        self.database = database
        self.embedding_provider = embedding_provider

    def index_memory(self, memory_id: str) -> bool:
        if self.embedding_provider is None:
            return False
        with self.database.session() as session:
            memory = session.get(MemoryRow, memory_id)
            if memory is None:
                return False
            document = f"{memory.title}\n{memory.content}"
            if hasattr(self.embedding_provider, "embed_documents"):
                vector = self.embedding_provider.embed_documents([document])[0]
            else:
                vector = self.embedding_provider.embed([document])[0]
            existing = session.scalar(
                select(EmbeddingRow).where(EmbeddingRow.memory_id == memory_id)
            )
            if existing is None:
                existing = EmbeddingRow(memory_id=memory_id)
                session.add(existing)
            existing.provider = self.embedding_provider.name
            existing.model = self.embedding_provider.model
            existing.dimensions = len(vector)
            existing.vector_json = vector
            existing.vector_blob = np.asarray(vector, dtype=np.float32).tobytes()
        return True

    def search(
        self,
        request: SearchRequest,
        *,
        allowed_scopes: set[tuple[str, str | None]] | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            candidate_ids: list[str] | None = None
            lexical: dict[str, float] = {}
            semantic: dict[str, float] = {}
            mode = "fts5"
            query = _fts_query(request.query)
            if query:
                fts_rows = session.execute(
                    text(
                        """
                        SELECT memory_id, bm25(memory_fts, 0.0, 5.0, 2.5, 1.0, 2.0, 1.0) AS rank
                        FROM memory_fts
                        WHERE memory_fts MATCH :query
                        ORDER BY rank
                        LIMIT :candidate_limit
                        """
                    ),
                    {"query": query, "candidate_limit": max(200, request.limit * 8)},
                ).all()
                candidate_ids = [str(row.memory_id) for row in fts_rows]
                lexical = {
                    str(row.memory_id): 1.0 / (1.0 + abs(float(row.rank))) for row in fts_rows
                }
                if not candidate_ids and self.embedding_provider is None:
                    return {"items": [], "total": 0, "mode": "fts5"}

            statement = select(MemoryRow)
            if candidate_ids is not None and self.embedding_provider is None:
                statement = statement.where(MemoryRow.id.in_(candidate_ids))
            if request.scope_type is not None:
                statement = statement.where(MemoryRow.scope_type == request.scope_type)
            if request.scope_key is not None:
                statement = statement.where(MemoryRow.scope_key == request.scope_key)
            if request.memory_type is not None:
                statement = statement.where(MemoryRow.memory_type == request.memory_type)
            if request.status is not None:
                statement = statement.where(MemoryRow.status == request.status)
            elif not request.include_history:
                statement = statement.where(MemoryRow.status == MemoryStatus.ACTIVE)

            memory_rows = list(session.scalars(statement))
            provider_failed = False
            if self.embedding_provider is not None and request.query.strip():
                try:
                    if hasattr(self.embedding_provider, "embed_query"):
                        embedded_query = self.embedding_provider.embed_query(request.query)
                    else:
                        embedded_query = self.embedding_provider.embed([request.query])[0]
                    embeddings = list(
                        session.scalars(
                            select(EmbeddingRow).where(
                                EmbeddingRow.provider == self.embedding_provider.name,
                                EmbeddingRow.model == self.embedding_provider.model,
                                EmbeddingRow.memory_id.in_([row.id for row in memory_rows]),
                            )
                        )
                    )
                    exact_index = ExactVectorIndex(
                        {
                            embedding.memory_id: list(embedding.vector_json or [])
                            for embedding in embeddings
                        }
                    )
                    semantic = {
                        memory_id: (cosine + 1.0) / 2.0
                        for memory_id, cosine in exact_index.search(
                            embedded_query, limit=len(embeddings)
                        )
                    }
                    if semantic:
                        mode = "hybrid"
                except ProviderError:
                    mode = "fts5-fallback"
                    provider_failed = True
            if provider_failed and candidate_ids is not None:
                candidate_id_set = set(candidate_ids)
                memory_rows = [row for row in memory_rows if row.id in candidate_id_set]
            if allowed_scopes is not None:
                memory_rows = [
                    row
                    for row in memory_rows
                    if (row.scope_type.value, row.scope_key) in allowed_scopes
                    or (row.scope_type.value, None) in allowed_scopes
                ]
            now = datetime.now(UTC)
            valid_rows: list[MemoryRow] = []
            for row in memory_rows:
                valid_from = row.valid_from
                valid_to = row.valid_to
                if (
                    valid_from
                    and (
                        valid_from.replace(tzinfo=UTC) if valid_from.tzinfo is None else valid_from
                    )
                    > now
                ):
                    continue
                if (
                    valid_to
                    and (valid_to.replace(tzinfo=UTC) if valid_to.tzinfo is None else valid_to)
                    <= now
                ):
                    continue
                valid_rows.append(row)

            def score(row: MemoryRow) -> float:
                created = (
                    row.created_at.replace(tzinfo=UTC)
                    if row.created_at.tzinfo is None
                    else row.created_at
                )
                age_days = max(0.0, (now - created).total_seconds() / 86400)
                recency = math.exp(-age_days / 90)
                lexical_score = lexical.get(row.id, 0.35 if not query else 0.0)
                semantic_score = semantic.get(row.id, 0.0)
                return (
                    lexical_score * 0.32
                    + semantic_score * 0.22
                    + _scope_weight(row.scope_type.value) * 0.18
                    + row.importance * 0.12
                    + recency * 0.08
                    + row.confidence * 0.08
                )

            ranked = sorted(valid_rows, key=score, reverse=True)
            total = len(ranked)
            page = ranked[request.offset : request.offset + request.limit]
            items = [
                {
                    "memory": self._serialize(row),
                    "score": round(score(row), 6),
                    "lexical_score": round(lexical.get(row.id, 0.0), 6),
                    "semantic_score": round(semantic.get(row.id, 0.0), 6),
                }
                for row in page
            ]
            return {"items": items, "total": total, "mode": mode}

    @staticmethod
    def _serialize(row: MemoryRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "scope_type": row.scope_type.value,
            "scope_key": row.scope_key,
            "memory_type": row.memory_type.value,
            "category": row.category,
            "subject": row.subject,
            "key": row.key,
            "title": row.title,
            "content": row.content,
            "status": row.status.value,
            "confidence": row.confidence,
            "importance": row.importance,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "ttl_seconds": row.ttl_seconds,
            "supersedes_id": row.supersedes_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "created_by": row.created_by.value,
            "sensitivity": row.sensitivity.value,
            "metadata": row.metadata_json,
        }
