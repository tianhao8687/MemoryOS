from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryos.evaluation.human_review_builder import (
    HumanReviewPackBuilder,
    generator_source_digest,
)
from memoryos.evaluation.human_review_models import (
    ReviewerConfidence,
    ReviewResponseDraft,
    SafetyDisposition,
    load_completed_review,
    load_human_review_pack,
    load_human_review_source_config,
    review_agreement,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_CONFIG = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "sources.json"
RUBRIC = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "RUBRIC.md"
PUBLIC_PACK = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "data"
PUBLIC_PACK_DIGEST = "ecf532c8ebbe7b3f9866623eab0e9fb53cd979abe486fb29359cb9ca7f20729f"


def _write_rows(path: Path, rows: list[ReviewResponseDraft]) -> None:
    encoded = "".join(
        json.dumps(
            row.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in rows
    )
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _completed_rows(
    bundle_path: Path, assignment_id: str, reviewer_id: str
) -> list[ReviewResponseDraft]:
    bundle = load_human_review_pack(bundle_path)
    return [
        ReviewResponseDraft(
            assignment_id=row.assignment_id,
            case_id=row.case_id,
            candidate_id=row.candidate_id,
            reviewer_id=reviewer_id,
            semantic_relevance=0,
            safety_disposition=SafetyDisposition.ALLOW,
            must_retrieve=False,
            reviewer_confidence=ReviewerConfidence.HIGH,
        )
        for row in bundle.response_templates[assignment_id]
    ]


def test_checked_in_review_pack_is_pinned_unlabeled_and_test_sealed() -> None:
    bundle = load_human_review_pack(PUBLIC_PACK)
    config = load_human_review_source_config(SOURCE_CONFIG)

    assert bundle.digest == PUBLIC_PACK_DIGEST
    assert bundle.manifest.source_config_sha256 == config.digest()
    assert bundle.manifest.generator_source_sha256 == generator_source_digest()
    assert bundle.manifest.label_tier == "pending_human_adjudication"
    assert bundle.manifest.status == "pilot_unlabeled"
    assert bundle.manifest.qrels_loaded_during_build is False
    assert bundle.manifest.test_split_sealed is True
    assert bundle.manifest.summary.cases == 61
    assert bundle.manifest.summary.judgments_per_reviewer == 1922
    assert bundle.manifest.summary.cases_by_partition == {
        "calibration": 48,
        "diagnostic": 1,
        "validation": 12,
    }
    assert {row.source_partition for row in bundle.source_map} == {
        "train",
        "dev",
        "diagnostic",
    }
    assert bundle.coupling_audit["status"] == "pilot_only"
    assert bundle.coupling_audit["external_real_workload_repository_ids"] == []


def test_review_pack_build_is_deterministic_and_never_reads_qrels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_human_review_source_config(SOURCE_CONFIG)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        if path.name == "qrels.jsonl":
            raise AssertionError("human review builder attempted to read scorer-only qrels")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    first = tmp_path / "first"
    second = tmp_path / "second"
    builder = HumanReviewPackBuilder()
    first_manifest = builder.build(
        config,
        repository_root=REPOSITORY_ROOT,
        rubric_path=RUBRIC,
        output_root=first,
    )
    second_manifest = builder.build(
        config,
        repository_root=REPOSITORY_ROOT,
        rubric_path=RUBRIC,
        output_root=second,
    )

    assert first_manifest.digest() == second_manifest.digest() == PUBLIC_PACK_DIGEST
    first_files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    second_files = sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    assert first_files == second_files
    assert all((first / path).read_bytes() == (second / path).read_bytes() for path in first_files)


def test_completed_reviews_require_full_independent_assignments(tmp_path: Path) -> None:
    bundle = load_human_review_pack(PUBLIC_PACK)
    left_path = tmp_path / "left.jsonl"
    right_path = tmp_path / "right.jsonl"
    left_rows = _completed_rows(PUBLIC_PACK, "reviewer-a", "human-alpha")
    right_rows = _completed_rows(PUBLIC_PACK, "reviewer-b", "human-beta")
    _write_rows(left_path, left_rows)
    _write_rows(right_path, right_rows)

    left = load_completed_review(bundle, left_path)
    right = load_completed_review(bundle, right_path)
    agreement = review_agreement(left, right)
    assert agreement["pairs"] == 1922
    assert agreement["full_decision_rate"] == 1.0

    _write_rows(left_path, left_rows[:-1])
    with pytest.raises(ValueError, match="does not cover"):
        load_completed_review(bundle, left_path)


def test_review_pack_loader_rejects_artifact_tampering(tmp_path: Path) -> None:
    config = load_human_review_source_config(SOURCE_CONFIG)
    output = tmp_path / "pack"
    HumanReviewPackBuilder().build(
        config,
        repository_root=REPOSITORY_ROOT,
        rubric_path=RUBRIC,
        output_root=output,
    )
    with (output / "blind" / "reviewer-a.jsonl").open("ab") as handle:
        handle.write(b"{}\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        load_human_review_pack(output)
