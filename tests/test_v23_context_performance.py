from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta

import pytest

from memoryos.context.atoms import CompressionPolicy, ContextAtom, exact_deduplicate
from memoryos.context.budget import build_bundles
from memoryos.context.delta import ContextSnapshot, plan_delta
from memoryos.context.token_meter import UnicodeHeuristicTokenCounter
from memoryos.domain.schemas import DetailLevel, FreshnessState, TruthState


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atom(
    index: int,
    *,
    canonical_key: str | None = None,
    bundle_key: str | None = None,
    truth_state: TruthState = TruthState.RESOLVED,
    source_ref: str | None = None,
) -> ContextAtom:
    memory_id = f"memory-{index:03d}"
    rendered = f"- [{memory_id} @ {_digest(f'atom-{index}')}] fact {index}"
    return ContextAtom(
        memory_id=memory_id,
        memory_ids=(memory_id,),
        claim_ids=(f"claim-{index:03d}",),
        memory_key=f"fixture.fact.{index}",
        memory_content=f"fact {index}",
        canonical_key=canonical_key or _digest(f"canonical-{index}"),
        bundle_key=bundle_key or _digest(f"bundle-{index}"),
        atom_sha256=_digest(f"atom-{index}"),
        detail_level=DetailLevel.FACT,
        compression_policy=CompressionPolicy.COMPRESSIBLE,
        rendered_text=rendered,
        fact_text=f"fact {index}",
        truth_state=truth_state,
        freshness=FreshnessState.FRESH,
        valid_from=None,
        valid_to=None,
        recorded_at=datetime(2026, 8, 15, tzinfo=UTC),
        evidence_count=1,
        source_refs=(source_ref or f"source-{index:03d}",),
        evidence_pointer_version=_digest(f"pointer-{index}"),
        utility=1.0,
        estimated_tokens=20,
        category="decision",
        section="DECISIONS",
        modality="decision",
        polarity="positive",
        status="active",
    )


@pytest.mark.v23
def test_max_source_exact_dedup_preserves_every_evidence_reference() -> None:
    counter = UnicodeHeuristicTokenCounter()
    canonical_key = _digest("same-canonical-fact")
    atoms = [
        _atom(index, canonical_key=canonical_key, source_ref=f"source-{index:03d}")
        for index in range(50)
    ]

    deduplicated, duplicate_of = exact_deduplicate(atoms, counter)

    assert len(deduplicated) == 1
    assert deduplicated[0].evidence_count == 50
    assert len(deduplicated[0].source_refs) == 50
    assert len(deduplicated[0].memory_ids) == 50
    assert len(duplicate_of) == 49


@pytest.mark.v23
def test_large_contested_component_is_one_required_atomic_bundle() -> None:
    bundle_key = _digest("large-contested-component")
    atoms = [
        _atom(
            index,
            bundle_key=bundle_key,
            truth_state=TruthState.CONTESTED,
        )
        for index in range(80)
    ]

    bundles = build_bundles(atoms, [])

    assert len(bundles) == 1
    assert bundles[0].required is True
    assert bundles[0].safety_required is True
    assert len(bundles[0].atoms) == 80
    assert "contested_complete" in bundles[0].reasons


@pytest.mark.slow
@pytest.mark.v23
def test_eighty_atom_delta_planning_remains_bounded_across_one_hundred_steps() -> None:
    current = [_atom(index) for index in range(80)]
    now = datetime(2026, 8, 15, tzinfo=UTC)
    previous = ContextSnapshot(
        id="context-0",
        base_snapshot_id=None,
        request_fingerprint=_digest("request"),
        scope_fingerprint=_digest("scope"),
        policy_hash=_digest("policy"),
        tokenizer_id="unicode-heuristic-v1",
        counter_kind="estimated",
        items=[atom.snapshot_item(index) for index, atom in enumerate(current)],
        full_text_sha256=_digest("full-0"),
        full_estimated_tokens=2000,
        created_at=now,
        expires_at=now + timedelta(days=7),
    )

    started = time.perf_counter()
    for step in range(100):
        changed_index = step % len(current)
        old = current[changed_index]
        current[changed_index] = old.model_copy(
            update={
                "atom_sha256": _digest(f"changed-{step}"),
                "evidence_pointer_version": _digest(f"changed-pointer-{step}"),
            }
        )
        delta = plan_delta(previous, current)
        assert len(delta.changed) == 1
        assert not delta.added
        assert not delta.removed
        assert delta.unchanged_count == 79
        previous = previous.model_copy(
            update={
                "id": f"context-{step + 1}",
                "base_snapshot_id": previous.id,
                "items": [atom.snapshot_item(index) for index, atom in enumerate(current)],
                "full_text_sha256": _digest(f"full-{step + 1}"),
            }
        )

    assert time.perf_counter() - started < 5.0
