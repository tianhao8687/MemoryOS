from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from memoryos.db.models import SourceAnchorRow
from memoryos.domain.schemas import FreshnessState
from memoryos.freshness.tree_sitter_adapter import language_for_path, locate_symbol
from memoryos.integrations.git import GitContext, discover_git_context


@dataclass(frozen=True)
class FreshnessResult:
    state: FreshnessState
    path: str
    head: str | None
    explanation: str
    current_excerpt: str | None = None
    line_start: int | None = None
    line_end: int | None = None


def classify_mutation(
    *,
    file_exists: bool,
    same_blob: bool,
    path_changed: bool,
    symbol_found: bool,
    excerpt_equivalent: bool,
    similarity: float,
) -> FreshnessState:
    if not file_exists:
        return FreshnessState.STALE
    if same_blob:
        return FreshnessState.MOVED if path_changed else FreshnessState.FRESH
    if symbol_found and excerpt_equivalent:
        return FreshnessState.MOVED if path_changed else FreshnessState.FRESH
    if symbol_found:
        return FreshnessState.SUSPECT if similarity >= 0.35 else FreshnessState.STALE
    return FreshnessState.STALE


def _git(root: Path, *args: str, allow_failure: bool = False) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git command with structured args
        ["git", "-C", str(root), *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode and not allow_failure:
        raise ValueError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _renamed_path(context: GitContext, anchor: SourceAnchorRow) -> str | None:
    diff = _git(
        context.root,
        "diff",
        "--name-status",
        "--find-renames=50%",
        f"{anchor.commit_sha}..{context.head}",
        allow_failure=True,
    )
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R") and parts[1] == anchor.path:
            return parts[2]
    return None


def compare_anchor(anchor: SourceAnchorRow, repository_path: Path | str) -> FreshnessResult:
    try:
        context = discover_git_context(repository_path)
    except Exception:
        return FreshnessResult(
            FreshnessState.UNKNOWN,
            anchor.path,
            None,
            "Repository is unavailable or unreadable.",
        )
    if context.stable_key != anchor.repository_stable_key:
        return FreshnessResult(
            FreshnessState.UNKNOWN,
            anchor.path,
            context.head,
            "Repository stable key does not match the anchor.",
        )
    if context.head == anchor.cached_head and anchor.checked_at is not None:
        return FreshnessResult(
            anchor.freshness_state,
            anchor.path,
            context.head,
            "Cached freshness result for the current HEAD.",
        )
    current_path = anchor.path
    absolute = context.root / current_path
    if not absolute.is_file():
        moved = _renamed_path(context, anchor)
        if moved:
            current_path = moved
            absolute = context.root / moved
        if not absolute.is_file():
            return FreshnessResult(
                FreshnessState.STALE,
                current_path,
                context.head,
                "Anchored file was deleted and could not be relocated.",
            )
    blob = _git(context.root, "rev-parse", f"HEAD:{current_path}", allow_failure=True)
    if blob and blob == anchor.blob_sha:
        state = FreshnessState.MOVED if current_path != anchor.path else FreshnessState.FRESH
        return FreshnessResult(
            state,
            current_path,
            context.head,
            "Git detected a pure path move with an unchanged blob."
            if state is FreshnessState.MOVED
            else "Git blob is unchanged.",
            current_excerpt=anchor.evidence_excerpt,
            line_start=anchor.line_start,
            line_end=anchor.line_end,
        )
    source = absolute.read_text(encoding="utf-8", errors="replace")
    symbol = (
        locate_symbol(source, language_for_path(current_path), anchor.symbol_fqn)
        if anchor.symbol_fqn
        else None
    )
    if symbol is not None:
        current = symbol.excerpt
        exact = _hash(current) == anchor.excerpt_hash or _normalized(current) == _normalized(
            anchor.evidence_excerpt
        )
        if exact:
            state = (
                FreshnessState.MOVED
                if current_path != anchor.path or symbol.line_start != anchor.line_start
                else FreshnessState.FRESH
            )
            return FreshnessResult(
                state,
                current_path,
                context.head,
                "Anchored symbol was relocated without semantic text change."
                if state is FreshnessState.MOVED
                else "Anchored symbol remains equivalent.",
                current_excerpt=current,
                line_start=symbol.line_start,
                line_end=symbol.line_end,
            )
        similarity = SequenceMatcher(
            None, _normalized(anchor.evidence_excerpt), _normalized(current)
        ).ratio()
        return FreshnessResult(
            FreshnessState.SUSPECT if similarity >= 0.35 else FreshnessState.STALE,
            current_path,
            context.head,
            f"Anchored symbol changed materially (similarity={similarity:.3f}).",
            current_excerpt=current,
            line_start=symbol.line_start,
            line_end=symbol.line_end,
        )
    if anchor.evidence_excerpt and anchor.evidence_excerpt in source:
        line_start = source[: source.index(anchor.evidence_excerpt)].count("\n") + 1
        return FreshnessResult(
            FreshnessState.MOVED,
            current_path,
            context.head,
            "Evidence excerpt still exists but moved within the file.",
            current_excerpt=anchor.evidence_excerpt,
            line_start=line_start,
            line_end=line_start + anchor.evidence_excerpt.count("\n"),
        )
    return FreshnessResult(
        FreshnessState.STALE,
        current_path,
        context.head,
        "Anchored evidence no longer exists in the current file.",
    )


def apply_freshness_result(anchor: SourceAnchorRow, result: FreshnessResult) -> None:
    anchor.freshness_state = result.state
    anchor.path = result.path
    anchor.cached_head = result.head
    anchor.checked_at = datetime.now(UTC)
    if result.current_excerpt is not None:
        anchor.evidence_excerpt = result.current_excerpt
        anchor.excerpt_hash = _hash(result.current_excerpt)
    if result.line_start is not None:
        anchor.line_start = result.line_start
    if result.line_end is not None:
        anchor.line_end = result.line_end
