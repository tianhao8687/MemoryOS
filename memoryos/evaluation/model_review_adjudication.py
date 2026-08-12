from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from memoryos.evaluation.human_review_models import (
    AdjudicatedReview,
    BlindReviewCandidate,
    HumanReviewPackBundle,
    ReviewIssueTag,
    ReviewResponseDraft,
    SafetyDisposition,
    load_adjudication,
    load_completed_review,
    load_human_review_pack,
    review_agreement,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelAdjudicationPacketRow(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(min_length=1, max_length=160)
    candidate_id: str = Field(min_length=1, max_length=160)
    partition: str = Field(min_length=1, max_length=80)
    source_kind: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=20_000)
    query_repository_id: str = Field(min_length=1, max_length=160)
    cutoff_time: str = Field(min_length=1, max_length=80)
    candidate: BlindReviewCandidate
    reviewer_a: ReviewResponseDraft
    reviewer_b: ReviewResponseDraft


class ModelAdjudicationOverride(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(min_length=1, max_length=160)
    candidate_id: str = Field(min_length=1, max_length=160)
    semantic_relevance: int = Field(ge=0, le=3)
    safety_disposition: SafetyDisposition
    must_retrieve: bool
    issue_tags: list[ReviewIssueTag] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=2000)


class ModelCaseDecision(StrictModel):
    semantic_relevance: list[int] = Field(min_length=1)
    must_retrieve_indices: list[int] = Field(default_factory=list)


class ModelAdjudicationPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adjudicator_id: str = Field(min_length=1, max_length=160)
    safety_strategy: Literal["strictest_visible"] = "strictest_visible"
    reviewed_all_packet_rows: Literal[True] = True
    cases: dict[str, ModelCaseDecision] = Field(min_length=1)


def draft_model_resolutions_from_plan(
    *,
    packet_path: Path,
    plan_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    packet_hash = _sha256(packet_path)
    plan = TypeAdapter(ModelAdjudicationPlan).validate_python(json.loads(plan_path.read_bytes()))
    if plan.packet_sha256 != packet_hash:
        raise ValueError("adjudication plan is pinned to another packet")
    packet = _load_jsonl(packet_path, ModelAdjudicationPacketRow)
    grouped: dict[str, list[ModelAdjudicationPacketRow]] = {}
    for row in packet:
        grouped.setdefault(row.case_id, []).append(row)
    if set(plan.cases) != set(grouped):
        missing = sorted(set(grouped) - set(plan.cases))
        extra = sorted(set(plan.cases) - set(grouped))
        raise ValueError(f"plan cases must exactly cover packet; missing={missing}, extra={extra}")
    resolutions: list[AdjudicatedReview] = []
    for case_id, rows in grouped.items():
        decision = plan.cases[case_id]
        if len(decision.semantic_relevance) != len(rows):
            raise ValueError(
                f"case {case_id} has {len(rows)} rows but "
                f"{len(decision.semantic_relevance)} relevance decisions"
            )
        required_indices = set(decision.must_retrieve_indices)
        if len(required_indices) != len(decision.must_retrieve_indices):
            raise ValueError(f"case {case_id} repeats a must_retrieve index")
        if any(index < 1 or index > len(rows) for index in required_indices):
            raise ValueError(f"case {case_id} contains an out-of-range must_retrieve index")
        for index, (row, relevance) in enumerate(
            zip(rows, decision.semantic_relevance, strict=True), start=1
        ):
            _, safety, _, issue_tags = _conservative_baseline(row)
            must_retrieve = index in required_indices
            if must_retrieve and (relevance < 2 or safety is not SafetyDisposition.ALLOW):
                raise ValueError(f"case {case_id} row {index} has an unsafe must_retrieve decision")
            reviewer_ids = sorted(
                {str(row.reviewer_a.reviewer_id), str(row.reviewer_b.reviewer_id)}
            )
            resolutions.append(
                AdjudicatedReview(
                    case_id=row.case_id,
                    candidate_id=row.candidate_id,
                    adjudicator_id=plan.adjudicator_id,
                    reviewer_ids=reviewer_ids,
                    semantic_relevance=relevance,
                    safety_disposition=safety,
                    must_retrieve=must_retrieve,
                    issue_tags=issue_tags,
                    rationale=_baseline_rationale(
                        row,
                        relevance=relevance,
                        safety=safety,
                        must_retrieve=must_retrieve,
                    ),
                )
            )
    output_hash = _write_jsonl(output_path, resolutions)
    return {
        "schema_version": "1.0",
        "status": "draft_model_resolutions",
        "packet_sha256": packet_hash,
        "plan_sha256": _sha256(plan_path),
        "output_sha256": output_hash,
        "rows": len(resolutions),
        "reviewed_all_packet_rows": plan.reviewed_all_packet_rows,
        "safety_strategy": plan.safety_strategy,
        "human_gold_claim": False,
    }


def draft_model_resolutions(
    *,
    packet_path: Path,
    overrides_path: Path,
    output_path: Path,
    adjudicator_id: str = "model-third-party-root-v1",
) -> dict[str, Any]:
    packet = _load_jsonl(packet_path, ModelAdjudicationPacketRow)
    overrides = _load_jsonl(overrides_path, ModelAdjudicationOverride)
    packet_index = {(row.case_id, row.candidate_id): row for row in packet}
    if len(packet_index) != len(packet):
        raise ValueError("adjudication packet contains duplicate case/candidate pairs")
    override_index = {(row.case_id, row.candidate_id): row for row in overrides}
    if len(override_index) != len(overrides):
        raise ValueError("override file contains duplicate case/candidate pairs")
    extra_overrides = sorted(set(override_index) - set(packet_index))
    if extra_overrides:
        raise ValueError(f"overrides reference pairs outside the packet: {extra_overrides[:10]}")
    resolutions: list[AdjudicatedReview] = []
    baseline_counts = {"baseline": 0, "explicit_override": 0}
    for pair, row in sorted(packet_index.items()):
        override = override_index.get(pair)
        if override is None:
            relevance, safety, must_retrieve, issue_tags = _conservative_baseline(row)
            rationale = _baseline_rationale(
                row,
                relevance=relevance,
                safety=safety,
                must_retrieve=must_retrieve,
            )
            baseline_counts["baseline"] += 1
        else:
            relevance = override.semantic_relevance
            safety = override.safety_disposition
            must_retrieve = override.must_retrieve
            issue_tags = override.issue_tags
            rationale = override.rationale
            baseline_counts["explicit_override"] += 1
        reviewer_ids = sorted({str(row.reviewer_a.reviewer_id), str(row.reviewer_b.reviewer_id)})
        resolutions.append(
            AdjudicatedReview(
                case_id=row.case_id,
                candidate_id=row.candidate_id,
                adjudicator_id=adjudicator_id,
                reviewer_ids=reviewer_ids,
                semantic_relevance=relevance,
                safety_disposition=safety,
                must_retrieve=must_retrieve,
                issue_tags=issue_tags,
                rationale=rationale,
            )
        )
    output_hash = _write_jsonl(output_path, resolutions)
    return {
        "schema_version": "1.0",
        "status": "draft_model_resolutions",
        "packet_sha256": _sha256(packet_path),
        "overrides_sha256": _sha256(overrides_path),
        "output_sha256": output_hash,
        "rows": len(resolutions),
        **baseline_counts,
        "human_gold_claim": False,
    }


def prepare_model_adjudication(
    *,
    dataset_root: Path,
    review_a_path: Path,
    review_b_path: Path,
    packet_path: Path,
) -> dict[str, Any]:
    bundle = load_human_review_pack(dataset_root)
    left = load_completed_review(bundle, review_a_path)
    right = load_completed_review(bundle, review_b_path)
    reviewer_ids, assignment_ids = _validate_independent_reviews(left, right)
    left_index = _index_review(left)
    right_index = _index_review(right)
    case_index = _case_index(bundle)
    disagreements: list[ModelAdjudicationPacketRow] = []
    for pair in sorted(left_index):
        left_row = left_index[pair]
        right_row = right_index[pair]
        if _core_decision(left_row) == _core_decision(right_row):
            continue
        case, candidate = case_index[pair]
        disagreements.append(
            ModelAdjudicationPacketRow(
                case_id=pair[0],
                candidate_id=pair[1],
                partition=case.partition.value,
                source_kind=case.source_kind.value,
                query=case.query,
                query_repository_id=case.query_repository_id,
                cutoff_time=case.cutoff_time.isoformat().replace("+00:00", "Z"),
                candidate=candidate,
                reviewer_a=left_row,
                reviewer_b=right_row,
            )
        )
    packet_hash = _write_jsonl(packet_path, disagreements)
    agreement = review_agreement(left, right)
    return {
        "schema_version": "1.0",
        "status": "awaiting_model_adjudication",
        "label_tier": "model_review_provisional",
        "dataset_id": bundle.manifest.dataset_id,
        "dataset_digest": bundle.digest,
        "reviewer_ids": sorted(reviewer_ids),
        "assignment_ids": sorted(assignment_ids),
        "review_sha256": {
            "review_a": _sha256(review_a_path),
            "review_b": _sha256(review_b_path),
        },
        "agreement": agreement,
        "core_disagreements": len(disagreements),
        "packet_path": str(packet_path),
        "packet_sha256": packet_hash,
        "human_gold_claim": False,
    }


def finalize_model_adjudication(
    *,
    dataset_root: Path,
    review_a_path: Path,
    review_b_path: Path,
    resolutions_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    bundle = load_human_review_pack(dataset_root)
    left = load_completed_review(bundle, review_a_path)
    right = load_completed_review(bundle, review_b_path)
    reviewer_ids, assignment_ids = _validate_independent_reviews(left, right)
    left_index = _index_review(left)
    right_index = _index_review(right)
    disagreement_pairs = {
        pair
        for pair in left_index
        if _core_decision(left_index[pair]) != _core_decision(right_index[pair])
    }
    resolutions = _load_jsonl(resolutions_path, AdjudicatedReview)
    resolution_index = {(row.case_id, row.candidate_id): row for row in resolutions}
    if len(resolution_index) != len(resolutions):
        raise ValueError("resolution file contains duplicate case/candidate pairs")
    if set(resolution_index) != disagreement_pairs:
        missing = sorted(disagreement_pairs - set(resolution_index))[:10]
        extra = sorted(set(resolution_index) - disagreement_pairs)[:10]
        raise ValueError(
            f"resolutions must cover exactly core disagreements; missing={missing}, extra={extra}"
        )
    adjudicator_ids = {row.adjudicator_id for row in resolutions}
    if len(adjudicator_ids) != 1:
        raise ValueError("all disagreement resolutions must use one adjudicator_id")
    adjudicator_id = next(iter(adjudicator_ids))
    if adjudicator_id in reviewer_ids:
        raise ValueError("adjudicator must differ from both reviewers")
    if any(set(row.reviewer_ids) != reviewer_ids for row in resolutions):
        raise ValueError("resolution reviewer_ids do not match the completed reviews")

    final_rows: list[AdjudicatedReview] = []
    for pair in sorted(left_index):
        if pair in resolution_index:
            final_rows.append(resolution_index[pair])
            continue
        left_row = left_index[pair]
        right_row = right_index[pair]
        assert left_row.semantic_relevance is not None
        assert left_row.safety_disposition is not None
        assert left_row.must_retrieve is not None
        issue_tags = sorted(
            set(left_row.issue_tags) | set(right_row.issue_tags), key=lambda tag: tag.value
        )
        final_rows.append(
            AdjudicatedReview(
                case_id=pair[0],
                candidate_id=pair[1],
                adjudicator_id=adjudicator_id,
                reviewer_ids=sorted(reviewer_ids),
                semantic_relevance=left_row.semantic_relevance,
                safety_disposition=left_row.safety_disposition,
                must_retrieve=left_row.must_retrieve,
                issue_tags=issue_tags,
                rationale=(
                    "Both independent model reviewers agreed on the relevance, safety, and "
                    "must-retrieve decision; the third-party model adjudicator accepted it."
                ),
            )
        )
    output_hash = _write_jsonl(output_path, final_rows)
    validated = load_adjudication(
        bundle,
        [review_a_path, review_b_path],
        output_path,
    )
    if len(validated) != len(final_rows):
        raise ValueError("written adjudication failed row-count validation")
    return {
        "schema_version": "1.0",
        "status": "model_adjudicated_provisional",
        "label_tier": "model_adjudicated_provisional",
        "dataset_id": bundle.manifest.dataset_id,
        "dataset_digest": bundle.digest,
        "reviewer_ids": sorted(reviewer_ids),
        "assignment_ids": sorted(assignment_ids),
        "adjudicator_id": adjudicator_id,
        "review_sha256": {
            "review_a": _sha256(review_a_path),
            "review_b": _sha256(review_b_path),
        },
        "resolutions_sha256": _sha256(resolutions_path),
        "adjudication_sha256": output_hash,
        "total_rows": len(final_rows),
        "core_disagreements_adjudicated": len(resolutions),
        "agreement": review_agreement(left, right),
        "human_gold_claim": False,
        "limitations": [
            "Both reviewers and the adjudicator are language models, not independent humans.",
            "The initial real-workload slice overlaps a Git-history source repository.",
            "This provisional artifact cannot approve production retrieval weights.",
        ],
    }


def _validate_independent_reviews(
    left: tuple[ReviewResponseDraft, ...],
    right: tuple[ReviewResponseDraft, ...],
) -> tuple[set[str], set[str]]:
    reviewer_ids = {str(left[0].reviewer_id), str(right[0].reviewer_id)}
    assignment_ids = {left[0].assignment_id, right[0].assignment_id}
    if len(reviewer_ids) != 2:
        raise ValueError("model reviews must use distinct reviewer ids")
    if len(assignment_ids) != 2:
        raise ValueError("model reviews must use distinct blind assignments")
    return reviewer_ids, assignment_ids


def _index_review(
    rows: tuple[ReviewResponseDraft, ...],
) -> dict[tuple[str, str], ReviewResponseDraft]:
    indexed = {(row.case_id, row.candidate_id): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("review contains duplicate case/candidate pairs")
    return indexed


def _case_index(
    bundle: HumanReviewPackBundle,
) -> dict[tuple[str, str], tuple[Any, BlindReviewCandidate]]:
    cases = next(iter(bundle.assignments.values()))
    return {
        (case.id, candidate.id): (case, candidate)
        for case in cases
        for candidate in case.candidates
    }


def _core_decision(row: ReviewResponseDraft) -> tuple[int | None, str | None, bool | None]:
    disposition = row.safety_disposition.value if row.safety_disposition is not None else None
    return row.semantic_relevance, disposition, row.must_retrieve


def _conservative_baseline(
    row: ModelAdjudicationPacketRow,
) -> tuple[int, SafetyDisposition, bool, list[ReviewIssueTag]]:
    left_relevance = row.reviewer_a.semantic_relevance
    right_relevance = row.reviewer_b.semantic_relevance
    left_safety = row.reviewer_a.safety_disposition
    right_safety = row.reviewer_b.safety_disposition
    left_required = row.reviewer_a.must_retrieve
    right_required = row.reviewer_b.must_retrieve
    assert left_relevance is not None and right_relevance is not None
    assert left_safety is not None and right_safety is not None
    assert left_required is not None and right_required is not None
    relevance = min(left_relevance, right_relevance)
    safety_order = {
        SafetyDisposition.ALLOW: 0,
        SafetyDisposition.UNCERTAIN: 1,
        SafetyDisposition.EXCLUDE: 2,
    }
    safety = max((left_safety, right_safety), key=safety_order.__getitem__)
    must_retrieve = left_required and right_required and relevance >= 2
    must_retrieve = must_retrieve and safety is SafetyDisposition.ALLOW
    issue_tags = sorted(
        set(row.reviewer_a.issue_tags) | set(row.reviewer_b.issue_tags),
        key=lambda tag: tag.value,
    )
    return relevance, safety, must_retrieve, issue_tags


def _baseline_rationale(
    row: ModelAdjudicationPacketRow,
    *,
    relevance: int,
    safety: SafetyDisposition,
    must_retrieve: bool,
) -> str:
    title = row.candidate.title
    query = row.query
    if relevance == 0:
        utility = (
            f'The visible candidate "{title}" supplies no actionable constraint for "{query}"; '
            "shared files or vocabulary alone are insufficient."
        )
    elif relevance == 1:
        utility = (
            f'The visible candidate "{title}" is related background for "{query}", but it does '
            "not materially constrain the implementation."
        )
    elif relevance == 2:
        utility = (
            f'The visible candidate "{title}" materially narrows an implementation or constraint '
            f'for "{query}".'
        )
    else:
        utility = (
            f'The visible candidate "{title}" is decisive evidence for "{query}" and is likely '
            "to change whether the task is completed correctly."
        )
    if safety is SafetyDisposition.UNCERTAIN:
        safety_text = (
            " Availability remains uncertain because the blind packet does not establish Git "
            "ancestry from timestamps alone."
        )
    elif safety is SafetyDisposition.EXCLUDE:
        safety_text = " The visible scope or validity evidence requires exclusion."
    else:
        safety_text = " No visible scope or validity evidence requires exclusion."
    required_text = (
        " Omitting it would materially weaken task success."
        if must_retrieve
        else " It is not individually required, including when equivalent evidence exists."
    )
    return utility + safety_text + required_text


def _load_jsonl[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"artifact is not UTF-8: {path}") from exc
    lines = text.splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError(f"artifact contains blank JSONL rows: {path}")
    adapter = TypeAdapter(model)
    values: list[ModelT] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            values.append(adapter.validate_python(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row {path}:{line_number}") from exc
    return values


def _write_jsonl(path: Path, rows: Sequence[BaseModel]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(
        json.dumps(
            row.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "ModelAdjudicationOverride",
    "ModelAdjudicationPacketRow",
    "ModelAdjudicationPlan",
    "ModelCaseDecision",
    "draft_model_resolutions",
    "draft_model_resolutions_from_plan",
    "finalize_model_adjudication",
    "prepare_model_adjudication",
]
