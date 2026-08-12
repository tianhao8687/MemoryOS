from __future__ import annotations

import json
from pathlib import Path

from memoryos.evaluation.human_review_models import (
    AdjudicatedReview,
    ReviewerConfidence,
    ReviewResponseDraft,
    SafetyDisposition,
    load_adjudication,
    load_human_review_pack,
)
from memoryos.evaluation.model_review_adjudication import (
    draft_model_resolutions_from_plan,
    finalize_model_adjudication,
    prepare_model_adjudication,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PACK = REPOSITORY_ROOT / "benchmarks" / "human_review_v1" / "data"


def _write_models(path: Path, rows: list[ReviewResponseDraft | AdjudicatedReview]) -> None:
    encoded = "".join(
        json.dumps(
            row.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in rows
    )
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _review_rows(
    assignment_id: str, reviewer_id: str, *, disagreement_pair: tuple[str, str] | None = None
) -> list[ReviewResponseDraft]:
    bundle = load_human_review_pack(PUBLIC_PACK)
    rows: list[ReviewResponseDraft] = []
    for template in bundle.response_templates[assignment_id]:
        relevance = 1 if (template.case_id, template.candidate_id) == disagreement_pair else 0
        rows.append(
            ReviewResponseDraft(
                assignment_id=template.assignment_id,
                case_id=template.case_id,
                candidate_id=template.candidate_id,
                reviewer_id=reviewer_id,
                semantic_relevance=relevance,
                safety_disposition=SafetyDisposition.ALLOW,
                must_retrieve=False,
                reviewer_confidence=ReviewerConfidence.HIGH,
            )
        )
    return rows


def test_model_adjudication_prepares_only_disagreements_and_never_claims_human_gold(
    tmp_path: Path,
) -> None:
    bundle = load_human_review_pack(PUBLIC_PACK)
    first_template = bundle.response_templates["reviewer-a"][0]
    disagreement_pair = (first_template.case_id, first_template.candidate_id)
    left_path = tmp_path / "left.jsonl"
    right_path = tmp_path / "right.jsonl"
    packet_path = tmp_path / "packet.jsonl"
    plan_path = tmp_path / "plan.json"
    resolution_path = tmp_path / "resolutions.jsonl"
    output_path = tmp_path / "adjudicated.jsonl"
    left = _review_rows("reviewer-a", "model-alpha")
    right = _review_rows("reviewer-b", "model-beta", disagreement_pair=disagreement_pair)
    _write_models(left_path, left)
    _write_models(right_path, right)

    prepared = prepare_model_adjudication(
        dataset_root=PUBLIC_PACK,
        review_a_path=left_path,
        review_b_path=right_path,
        packet_path=packet_path,
    )
    assert prepared["core_disagreements"] == 1
    assert prepared["human_gold_claim"] is False
    assert len(packet_path.read_text(encoding="utf-8").splitlines()) == 1

    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "packet_sha256": prepared["packet_sha256"],
                "adjudicator_id": "model-root",
                "safety_strategy": "strictest_visible",
                "reviewed_all_packet_rows": True,
                "cases": {
                    disagreement_pair[0]: {
                        "semantic_relevance": [1],
                        "must_retrieve_indices": [],
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    drafted = draft_model_resolutions_from_plan(
        packet_path=packet_path,
        plan_path=plan_path,
        output_path=resolution_path,
    )
    assert drafted["rows"] == 1
    assert drafted["reviewed_all_packet_rows"] is True
    finalized = finalize_model_adjudication(
        dataset_root=PUBLIC_PACK,
        review_a_path=left_path,
        review_b_path=right_path,
        resolutions_path=resolution_path,
        output_path=output_path,
    )
    assert finalized["status"] == "model_adjudicated_provisional"
    assert finalized["human_gold_claim"] is False
    assert finalized["core_disagreements_adjudicated"] == 1
    assert finalized["total_rows"] == 1922
    validated = load_adjudication(bundle, [left_path, right_path], output_path)
    assert len(validated) == 1922
