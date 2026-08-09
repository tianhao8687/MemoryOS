from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from memoryos.db.models import AnnIndexStateRow, EmbeddingRow, MemoryHealthRow, MemoryRow
from memoryos.db.session import Database
from memoryos.domain.schemas import MemoryStatus, MemoryTemperature, SearchRequest
from memoryos.errors import ProviderError
from memoryos.providers.base import EmbeddingProvider
from memoryos.retrieval.ann import OptionalSqliteAnnIndex
from memoryos.retrieval.vector import ExactVectorIndex

TOKEN_RE = re.compile(r"[\w.]+", re.UNICODE)


def _fts_query(query: str) -> str:
    tokens = TOKEN_RE.findall(query.lower())
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:24])


def _scope_weight(scope_type: str) -> float:
    return {"task": 1.0, "branch": 0.92, "repository": 0.84, "workspace": 0.72, "user": 0.62}.get(
        scope_type, 0.5
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RetrievalEngine:
    """FTS + persistent sqlite-vec retrieval with an exact, observable fallback."""

    def __init__(
        self, database: Database, embedding_provider: EmbeddingProvider | None = None
    ) -> None:
        self.database = database
        self.embedding_provider = embedding_provider
        self._ann_indexes: dict[str, OptionalSqliteAnnIndex] = {}

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
            session.flush()
            index, namespace = self._ann_index(
                self.embedding_provider.name,
                self.embedding_provider.model,
                len(vector),
            )
            written = index.upsert({memory_id: vector}) if index.available else 0
            self._save_ann_state(
                session,
                namespace=namespace,
                index=index,
                provider=self.embedding_provider.name,
                model=self.embedding_provider.model,
                dimensions=len(vector),
                item_count=index.count() if written else 0,
            )
        return True

    def search(
        self,
        request: SearchRequest,
        *,
        allowed_scopes: set[tuple[str, str | None]] | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            fts_ids: list[str] = []
            lexical: dict[str, float] = {}
            semantic: dict[str, float] = {}
            mode = "fts5"
            query = _fts_query(request.query)
            candidate_limit = max(200, request.limit * 8)
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
                    {"query": query, "candidate_limit": candidate_limit},
                ).all()
                fts_ids = [str(row.memory_id) for row in fts_rows]
                lexical = {
                    str(row.memory_id): 1.0 / (1.0 + abs(float(row.rank))) for row in fts_rows
                }

            provider_failed = False
            if self.embedding_provider is not None and request.query.strip():
                try:
                    embedded_query = (
                        self.embedding_provider.embed_query(request.query)
                        if hasattr(self.embedding_provider, "embed_query")
                        else self.embedding_provider.embed([request.query])[0]
                    )
                    semantic, vector_mode = self._semantic_search(
                        session,
                        embedded_query,
                        limit=max(1000, request.limit * 50),
                    )
                    if semantic:
                        mode = f"hybrid-{vector_mode}"
                    elif vector_mode == "exact-fallback":
                        mode = "fts5-exact-fallback"
                except ProviderError:
                    mode = "fts5-fallback"
                    provider_failed = True

            candidate_pool: set[str] | None = None
            if query:
                candidate_pool = set(fts_ids)
                if not provider_failed:
                    candidate_pool.update(semantic)
                if not candidate_pool:
                    return {"items": [], "total": 0, "mode": mode}

            statement = select(MemoryRow)
            if candidate_pool is not None:
                statement = statement.where(MemoryRow.id.in_(candidate_pool))
            if request.scope_type is not None:
                statement = statement.where(MemoryRow.scope_type == request.scope_type)
            if request.scope_key is not None:
                statement = statement.where(MemoryRow.scope_key == request.scope_key)
            if request.memory_type is not None:
                statement = statement.where(MemoryRow.memory_type == request.memory_type)
            if request.status is not None:
                statement = statement.where(MemoryRow.status == request.status)
            elif not request.include_history:
                statement = statement.outerjoin(
                    MemoryHealthRow, MemoryHealthRow.memory_id == MemoryRow.id
                ).where(
                    MemoryRow.status == MemoryStatus.ACTIVE,
                    or_(
                        MemoryHealthRow.memory_id.is_(None),
                        MemoryHealthRow.temperature != MemoryTemperature.ARCHIVED,
                    ),
                )

            memory_rows = list(session.scalars(statement))
            if allowed_scopes is not None:
                memory_rows = [
                    row
                    for row in memory_rows
                    if (row.scope_type.value, row.scope_key) in allowed_scopes
                    or (row.scope_type.value, None) in allowed_scopes
                ]
            valid_moment = request.as_of_valid_time or datetime.now(UTC)
            valid_rows = [
                row
                for row in memory_rows
                if (row.valid_from is None or _utc(row.valid_from) <= _utc(valid_moment))
                and (row.valid_to is None or _utc(valid_moment) < _utc(row.valid_to))
            ]
            now = datetime.now(UTC)

            def score(row: MemoryRow) -> float:
                age_days = max(0.0, (now - _utc(row.created_at)).total_seconds() / 86400)
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

    def _semantic_search(
        self,
        session: Session,
        query: list[float],
        *,
        limit: int,
    ) -> tuple[dict[str, float], str]:
        assert self.embedding_provider is not None
        provider = self.embedding_provider.name
        model = self.embedding_provider.model
        dimensions = len(query)
        index, namespace = self._ann_index(provider, model, dimensions)
        if index.available:
            expected = int(
                session.scalar(
                    select(func.count())
                    .select_from(EmbeddingRow)
                    .where(
                        EmbeddingRow.provider == provider,
                        EmbeddingRow.model == model,
                        EmbeddingRow.dimensions == dimensions,
                    )
                )
                or 0
            )
            if index.count() != expected:
                self._rebuild_namespace(
                    session,
                    index=index,
                    namespace=namespace,
                    provider=provider,
                    model=model,
                    dimensions=dimensions,
                )
            scores = dict(index.search(query, limit=min(limit, max(1, expected))))
            self._save_ann_state(
                session,
                namespace=namespace,
                index=index,
                provider=provider,
                model=model,
                dimensions=dimensions,
                item_count=index.count(),
            )
            return scores, "ann"

        embeddings = list(
            session.scalars(
                select(EmbeddingRow).where(
                    EmbeddingRow.provider == provider,
                    EmbeddingRow.model == model,
                    EmbeddingRow.dimensions == dimensions,
                )
            )
        )
        exact = ExactVectorIndex({row.memory_id: list(row.vector_json or []) for row in embeddings})
        scores = {
            memory_id: (cosine + 1.0) / 2.0
            for memory_id, cosine in exact.search(query, limit=min(limit, len(embeddings)))
        }
        self._save_ann_state(
            session,
            namespace=namespace,
            index=index,
            provider=provider,
            model=model,
            dimensions=dimensions,
            item_count=0,
        )
        return scores, "exact-fallback"

    def rebuild_ann_index(self) -> dict[str, Any]:
        if self.embedding_provider is None:
            return {"status": "unconfigured", "namespaces": []}
        provider = self.embedding_provider.name
        model = self.embedding_provider.model
        with self.database.session() as session:
            dimensions = list(
                session.scalars(
                    select(EmbeddingRow.dimensions)
                    .where(
                        EmbeddingRow.provider == provider,
                        EmbeddingRow.model == model,
                    )
                    .distinct()
                )
            )
            results = []
            for dimension in dimensions:
                index, namespace = self._ann_index(provider, model, dimension)
                count = self._rebuild_namespace(
                    session,
                    index=index,
                    namespace=namespace,
                    provider=provider,
                    model=model,
                    dimensions=dimension,
                )
                results.append(
                    {
                        "namespace": namespace,
                        "dimensions": dimension,
                        "count": count,
                        "status": "ready" if index.available else "unavailable",
                        "reason": index.unavailable_reason,
                    }
                )
            return {"status": "ready" if results else "empty", "namespaces": results}

    def vector_status(self) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AnnIndexStateRow).order_by(AnnIndexStateRow.updated_at.desc())
                )
            )
            return [
                {
                    "namespace": row.namespace,
                    "backend": row.backend,
                    "provider": row.provider,
                    "model": row.model,
                    "model_fingerprint": row.model_fingerprint,
                    "dimensions": row.dimensions,
                    "item_count": row.item_count,
                    "status": row.status,
                    "unavailable_reason": row.unavailable_reason,
                    "last_rebuild_at": row.last_rebuild_at.isoformat()
                    if row.last_rebuild_at
                    else None,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    def close(self) -> None:
        for index in self._ann_indexes.values():
            index.close()
        self._ann_indexes.clear()

    def _ann_index(
        self,
        provider: str,
        model: str,
        dimensions: int,
    ) -> tuple[OptionalSqliteAnnIndex, str]:
        fingerprint = hashlib.sha256(f"{provider}|{model}".encode()).hexdigest()
        namespace = f"{provider}:{model}:{dimensions}:{fingerprint[:12]}"
        index = self._ann_indexes.get(namespace)
        if index is None:
            path = self.database.settings.ann_dir / f"{fingerprint[:20]}-{dimensions}.sqlite"
            index = OptionalSqliteAnnIndex(
                path,
                dimensions,
                enabled=self.database.settings.ann_enabled,
            )
            self._ann_indexes[namespace] = index
        return index, namespace

    def _rebuild_namespace(
        self,
        session: Session,
        *,
        index: OptionalSqliteAnnIndex,
        namespace: str,
        provider: str,
        model: str,
        dimensions: int,
    ) -> int:
        if not index.available or not index.clear():
            self._save_ann_state(
                session,
                namespace=namespace,
                index=index,
                provider=provider,
                model=model,
                dimensions=dimensions,
                item_count=0,
            )
            return 0
        embeddings = list(
            session.scalars(
                select(EmbeddingRow).where(
                    EmbeddingRow.provider == provider,
                    EmbeddingRow.model == model,
                    EmbeddingRow.dimensions == dimensions,
                )
            )
        )
        count = index.upsert({row.memory_id: list(row.vector_json or []) for row in embeddings})
        self._save_ann_state(
            session,
            namespace=namespace,
            index=index,
            provider=provider,
            model=model,
            dimensions=dimensions,
            item_count=count,
            rebuilt=True,
        )
        return count

    @staticmethod
    def _save_ann_state(
        session: Session,
        *,
        namespace: str,
        index: OptionalSqliteAnnIndex,
        provider: str,
        model: str,
        dimensions: int,
        item_count: int,
        rebuilt: bool = False,
    ) -> None:
        row = session.get(AnnIndexStateRow, namespace)
        now = datetime.now(UTC)
        if row is None:
            row = AnnIndexStateRow(
                namespace=namespace,
                backend=index.name,
                provider=provider,
                model=model,
                model_fingerprint=hashlib.sha256(f"{provider}|{model}".encode()).hexdigest(),
                dimensions=dimensions,
                item_count=item_count,
                status="ready" if index.available else "unavailable",
                unavailable_reason=index.unavailable_reason,
                last_rebuild_at=now if rebuilt else None,
                updated_at=now,
            )
            session.add(row)
            return
        row.item_count = item_count
        row.status = "ready" if index.available else "unavailable"
        row.unavailable_reason = index.unavailable_reason
        row.updated_at = now
        if rebuilt:
            row.last_rebuild_at = now

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
