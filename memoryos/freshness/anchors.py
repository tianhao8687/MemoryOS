from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from memoryos.claims.truth import TruthMaintenanceService
from memoryos.db.models import (
    AuditEventRow,
    ClaimEvidenceRow,
    ClaimRow,
    SourceAnchorRow,
)
from memoryos.db.session import Database
from memoryos.domain.schemas import ClaimStaleState, FreshnessState
from memoryos.errors import NotFoundError
from memoryos.freshness.git_compare import apply_freshness_result, compare_anchor
from memoryos.freshness.tree_sitter_adapter import language_for_path, locate_symbol
from memoryos.integrations.git import discover_git_context


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blob(root: Path, path: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git command with structured args
        ["git", "-C", str(root), "rev-parse", f"HEAD:{path}"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise NotFoundError(f"path is not tracked at HEAD: {path}")
    return completed.stdout.strip()


class SourceAnchorService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.truth = TruthMaintenanceService()

    def create(
        self,
        *,
        memory_id: str,
        repository_path: Path | str,
        path: str,
        symbol_fqn: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> dict[str, Any]:
        context = discover_git_context(repository_path)
        relative = Path(path).as_posix().lstrip("/")
        absolute = (context.root / relative).resolve()
        if context.root not in absolute.parents or not absolute.is_file():
            raise NotFoundError("anchor path must be a file inside the selected repository")
        source = absolute.read_text(encoding="utf-8", errors="replace")
        language = language_for_path(relative)
        symbol = locate_symbol(source, language, symbol_fqn) if symbol_fqn else None
        if symbol is not None:
            excerpt = symbol.excerpt
            line_start = symbol.line_start
            line_end = symbol.line_end
            symbol_kind = symbol.symbol_kind
        else:
            lines = source.splitlines()
            start = max(1, line_start or 1)
            end = min(len(lines), line_end or min(len(lines), start + 79))
            excerpt = "\n".join(lines[start - 1 : end])
            line_start, line_end, symbol_kind = start, end, None
        excerpt = excerpt[:10000]
        context_text = "\n".join(source.splitlines()[max(0, line_start - 4) : line_end + 3])
        observed_at = datetime.now(UTC)
        with self.database.session() as session:
            claim_ids = list(
                session.scalars(select(ClaimRow.id).where(ClaimRow.memory_id == memory_id))
            )
            if not claim_ids:
                raise NotFoundError("memory has no claim to anchor")
            anchor = SourceAnchorRow(
                repository_stable_key=context.stable_key,
                commit_sha=context.head,
                path=relative,
                blob_sha=_blob(context.root, relative),
                language=language,
                symbol_fqn=symbol_fqn,
                symbol_kind=symbol_kind,
                line_start=line_start,
                line_end=line_end,
                evidence_excerpt=excerpt,
                excerpt_hash=_hash(excerpt),
                context_hash=_hash(context_text),
                freshness_state=FreshnessState.FRESH,
                cached_head=context.head,
                checked_at=observed_at,
                observed_head=context.head,
                observed_path=relative,
                observed_line_start=line_start,
                observed_line_end=line_end,
                observed_excerpt_hash=_hash(excerpt),
                observed_at=observed_at,
                metadata_json={
                    "bounded_excerpt": True,
                    "parser_backend": symbol.backend if symbol is not None else "bounded-lines",
                },
            )
            session.add(anchor)
            session.flush()
            evidence_rows = list(
                session.scalars(
                    select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id.in_(claim_ids))
                )
            )
            for evidence in evidence_rows:
                evidence.source_anchor_id = anchor.id
            self.truth.mark_memory_stale(
                session,
                memory_id,
                ClaimStaleState.FRESH,
                actor="manual:source-anchor",
            )
            session.add(
                AuditEventRow(
                    action="source_anchor_create",
                    entity_type="memory",
                    entity_id=memory_id,
                    actor="manual",
                    details={"anchor_id": anchor.id, "path": relative, "symbol": symbol_fqn},
                )
            )
            session.flush()
            return self._serialize(anchor)

    def refresh(self, *, memory_id: str, repository_path: Path | str) -> dict[str, Any]:
        with self.database.session() as session:
            anchors = list(
                session.scalars(
                    select(SourceAnchorRow)
                    .join(
                        ClaimEvidenceRow,
                        ClaimEvidenceRow.source_anchor_id == SourceAnchorRow.id,
                    )
                    .join(ClaimRow, ClaimRow.id == ClaimEvidenceRow.claim_id)
                    .where(ClaimRow.memory_id == memory_id)
                    .distinct()
                )
            )
            if not anchors:
                raise NotFoundError("memory has no source anchor")
            results = []
            for anchor in anchors:
                result = compare_anchor(anchor, repository_path)
                apply_freshness_result(anchor, result)
                results.append(
                    {
                        **self._serialize(anchor),
                        "explanation": result.explanation,
                        "current_excerpt": result.current_excerpt,
                    }
                )
            aggregate = max(
                (anchor.freshness_state for anchor in anchors),
                key=lambda value: {
                    FreshnessState.FRESH: 0,
                    FreshnessState.MOVED: 1,
                    FreshnessState.UNKNOWN: 2,
                    FreshnessState.SUSPECT: 3,
                    FreshnessState.STALE: 4,
                }[value],
            )
            claim_state = {
                FreshnessState.FRESH: ClaimStaleState.FRESH,
                FreshnessState.MOVED: ClaimStaleState.FRESH,
                FreshnessState.SUSPECT: ClaimStaleState.SUSPECT,
                FreshnessState.STALE: ClaimStaleState.STALE,
                FreshnessState.UNKNOWN: ClaimStaleState.UNKNOWN,
            }[aggregate]
            self.truth.mark_memory_stale(session, memory_id, claim_state)
            session.add(
                AuditEventRow(
                    action="memory_refresh",
                    entity_type="memory",
                    entity_id=memory_id,
                    actor="manual",
                    details={"freshness": aggregate.value, "anchors": len(anchors)},
                )
            )
            return {
                "memory_id": memory_id,
                "freshness": aggregate.value,
                "anchors": results,
                "replacement_candidate": (
                    {
                        "status": "candidate",
                        "reason": "Re-capture current evidence before replacing stale memory.",
                        "evidence": next(
                            (
                                item["current_excerpt"]
                                for item in results
                                if item["current_excerpt"]
                            ),
                            None,
                        ),
                    }
                    if aggregate in {FreshnessState.SUSPECT, FreshnessState.STALE}
                    else None
                ),
            }

    @staticmethod
    def _serialize(anchor: SourceAnchorRow) -> dict[str, Any]:
        observed_path = anchor.observed_path or anchor.path
        return {
            "id": anchor.id,
            "repository_stable_key": anchor.repository_stable_key,
            "commit_sha": anchor.commit_sha,
            "path": observed_path,
            "original_path": anchor.path,
            "observed_path": observed_path,
            "blob_sha": anchor.blob_sha,
            "language": anchor.language,
            "symbol_fqn": anchor.symbol_fqn,
            "symbol_kind": anchor.symbol_kind,
            "line_start": anchor.observed_line_start,
            "line_end": anchor.observed_line_end,
            "original_line_start": anchor.line_start,
            "original_line_end": anchor.line_end,
            "observed_line_start": anchor.observed_line_start,
            "observed_line_end": anchor.observed_line_end,
            "excerpt_hash": anchor.excerpt_hash,
            "original_excerpt_hash": anchor.excerpt_hash,
            "observed_excerpt_hash": anchor.observed_excerpt_hash,
            "context_hash": anchor.context_hash,
            "freshness_state": anchor.freshness_state.value,
            "cached_head": anchor.cached_head,
            "checked_at": anchor.checked_at.isoformat() if anchor.checked_at else None,
            "observed_head": anchor.observed_head,
            "observed_at": anchor.observed_at.isoformat() if anchor.observed_at else None,
            "parser_backend": anchor.metadata_json.get("parser_backend", "unknown"),
        }
