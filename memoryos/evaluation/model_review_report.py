from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast

from memoryos.evaluation.calibration_models import (
    CalibrationJudgment,
    CalibrationSplit,
    load_calibration_dataset,
)
from memoryos.evaluation.human_review_models import (
    AdjudicatedReview,
    ReviewResponseDraft,
    load_adjudication,
    load_completed_review,
    load_human_review_pack,
    review_agreement,
)
from memoryos.evaluation.model_review_adjudication import (
    draft_model_resolutions_from_plan,
    finalize_model_adjudication,
    prepare_model_adjudication,
)

type Label = str | int | bool
type Pair = tuple[str, str]


def analyze_model_review(
    *,
    dataset_root: Path,
    calibration_root: Path,
    review_a_path: Path,
    review_b_path: Path,
    adjudication_path: Path,
) -> dict[str, Any]:
    """Analyze a completed model-only review without promoting it to human gold."""

    bundle = load_human_review_pack(dataset_root)
    left = load_completed_review(bundle, review_a_path)
    right = load_completed_review(bundle, review_b_path)
    adjudicated = load_adjudication(
        bundle,
        [review_a_path, review_b_path],
        adjudication_path,
    )
    left_index = _index_review(left)
    right_index = _index_review(right)
    final_index = _index_adjudication(adjudicated)
    ordered_pairs = sorted(final_index)
    case_partition = {
        case.id: case.partition.value for case in next(iter(bundle.assignments.values()))
    }

    relevance_left = [_complete_relevance(left_index[pair]) for pair in ordered_pairs]
    relevance_right = [_complete_relevance(right_index[pair]) for pair in ordered_pairs]
    safety_left = [_complete_safety(left_index[pair]) for pair in ordered_pairs]
    safety_right = [_complete_safety(right_index[pair]) for pair in ordered_pairs]
    required_left = [_complete_required(left_index[pair]) for pair in ordered_pairs]
    required_right = [_complete_required(right_index[pair]) for pair in ordered_pairs]
    exact_agreement = review_agreement(left, right)
    exact_agreement.update(
        {
            "relevance_cohen_kappa": _cohen_kappa(
                relevance_left,
                relevance_right,
                categories=[0, 1, 2, 3],
            ),
            "relevance_linear_weighted_kappa": _weighted_kappa(
                relevance_left,
                relevance_right,
                categories=[0, 1, 2, 3],
                quadratic=False,
            ),
            "relevance_quadratic_weighted_kappa": _weighted_kappa(
                relevance_left,
                relevance_right,
                categories=[0, 1, 2, 3],
                quadratic=True,
            ),
            "safety_cohen_kappa": _cohen_kappa(
                safety_left,
                safety_right,
                categories=["allow", "uncertain", "exclude"],
            ),
            "must_retrieve_cohen_kappa": _cohen_kappa(
                required_left,
                required_right,
                categories=[False, True],
            ),
        }
    )

    disagreements = [
        pair
        for pair in ordered_pairs
        if _core_decision(left_index[pair]) != _core_decision(right_index[pair])
    ]
    resolution_outcomes: Counter[str] = Counter()
    for pair in disagreements:
        left_decision = _core_decision(left_index[pair])
        right_decision = _core_decision(right_index[pair])
        final_decision = _core_decision(final_index[pair])
        if final_decision == left_decision:
            resolution_outcomes["reviewer_a"] += 1
        elif final_decision == right_decision:
            resolution_outcomes["reviewer_b"] += 1
        else:
            resolution_outcomes["neither_exactly"] += 1

    partition_metrics: dict[str, dict[str, Any]] = {}
    for partition in ("calibration", "validation", "diagnostic"):
        rows = [final_index[pair] for pair in ordered_pairs if case_partition[pair[0]] == partition]
        partition_metrics[partition] = _adjudication_distribution(rows)

    calibration = load_calibration_dataset(calibration_root)
    silver_index = {
        (judgment.query_id, judgment.candidate_id): judgment
        for split in CalibrationSplit
        for judgment in calibration.judgments[split]
    }
    silver_pairs: list[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]] = []
    for source_row in bundle.source_map:
        if source_row.source_dataset_id != calibration.manifest.dataset_id:
            continue
        source_pair = (source_row.source_query_id, source_row.source_candidate_id)
        try:
            judgment = silver_index[source_pair]
        except KeyError as exc:
            raise ValueError(
                f"source map references missing silver judgment: {source_pair}"
            ) from exc
        pair = (source_row.case_id, source_row.candidate_id)
        silver_pairs.append(
            (
                pair,
                final_index[pair],
                judgment,
                source_row.source_repository_id,
            )
        )
    if not silver_pairs:
        raise ValueError("model review has no rows mapped to the calibration dataset")

    reviewer_silver_diagnostics = {
        "reviewer_a": _reviewer_silver_metrics(left_index, silver_pairs),
        "reviewer_b": _reviewer_silver_metrics(right_index, silver_pairs),
        "adjudicated": _adjudicated_silver_metrics(silver_pairs),
    }
    silver_diagnostic = {
        "comparison_role": "post_adjudication_proxy_diagnostic_not_ground_truth",
        "rows": len(silver_pairs),
        "reviewer_comparison": reviewer_silver_diagnostics,
        "relevance_confusion_silver_to_adjudicated": _relevance_confusion(silver_pairs),
        "safety_by_silver_eligibility": _safety_by_eligibility(silver_pairs),
        "must_retrieve_by_silver_required": _must_by_required(silver_pairs),
        "repository_slices": _silver_repository_slices(silver_pairs),
        "interpretation": (
            "Silver labels measure Git path overlap; adjudicated model labels measure visible "
            "downstream utility. Divergence is evidence against treating the proxy as human gold, "
            "not proof that either label source is correct."
        ),
    }

    reviewer_ids = sorted({str(left[0].reviewer_id), str(right[0].reviewer_id)})
    adjudicator_ids = sorted({row.adjudicator_id for row in adjudicated})
    if len(adjudicator_ids) != 1:
        raise ValueError("adjudication must use exactly one adjudicator")
    return {
        "schema_version": "1.0",
        "status": "model_adjudicated_provisional",
        "label_tier": "model_adjudicated_provisional",
        "human_gold_claim": False,
        "dataset_id": bundle.manifest.dataset_id,
        "dataset_digest": bundle.digest,
        "test_split_sealed": bundle.manifest.test_split_sealed,
        "reviewer_ids": reviewer_ids,
        "adjudicator_id": adjudicator_ids[0],
        "total_rows": len(adjudicated),
        "core_disagreements_adjudicated": len(disagreements),
        "agreement": exact_agreement,
        "resolution_outcomes": dict(sorted(resolution_outcomes.items())),
        "reviewer_distributions": {
            "reviewer_a": _review_distribution(left),
            "reviewer_b": _review_distribution(right),
        },
        "adjudicated_distribution": _adjudication_distribution(adjudicated),
        "partition_slices": partition_metrics,
        "silver_diagnostic": silver_diagnostic,
        "limitations": [
            "Both effective reviewers and the adjudicator are language models, not humans.",
            "High raw relevance agreement is affected by the dominant relevance=0 class.",
            "The only real-workload case overlaps a Git-history repository and is diagnostic.",
            "This artifact must not fit or approve production retrieval weights.",
        ],
    }


def publish_model_review_bundle(
    *,
    dataset_root: Path,
    calibration_root: Path,
    review_a_path: Path,
    review_b_path: Path,
    policy_path: Path,
    packet_path: Path,
    plan_path: Path,
    resolutions_path: Path,
    adjudication_path: Path,
    protocol_audit_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate, reproduce, and publish an auditable model-only review bundle."""

    _verify_reproducible_chain(
        dataset_root=dataset_root,
        review_a_path=review_a_path,
        review_b_path=review_b_path,
        packet_path=packet_path,
        plan_path=plan_path,
        resolutions_path=resolutions_path,
        adjudication_path=adjudication_path,
    )
    protocol_audit = _load_json_object(protocol_audit_path)
    if protocol_audit.get("human_gold_claim") is not False:
        raise ValueError("protocol audit must explicitly reject a human-gold claim")

    output_root.mkdir(parents=True, exist_ok=True)
    sources = {
        "reviewer_a": (review_a_path, "reviewer-a2.responses.jsonl"),
        "reviewer_b": (review_b_path, "reviewer-b.responses.jsonl"),
        "adjudication_policy": (policy_path, "ADJUDICATION_POLICY.md"),
        "disagreement_packet": (packet_path, "disagreement-packet.jsonl"),
        "decision_plan": (plan_path, "decision-plan.json"),
        "resolutions": (resolutions_path, "resolutions.jsonl"),
        "adjudicated": (adjudication_path, "adjudicated-provisional.jsonl"),
        "protocol_audit": (protocol_audit_path, "protocol-audit.json"),
    }
    inventory: dict[str, dict[str, Any]] = {}
    for name, (source, relative_name) in sources.items():
        raw = _canonical_text_bytes(source)
        destination = output_root / relative_name
        destination.write_bytes(raw)
        inventory[name] = {
            "path": relative_name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "rows": _artifact_rows(relative_name, raw),
        }

    analysis = analyze_model_review(
        dataset_root=dataset_root,
        calibration_root=calibration_root,
        review_a_path=output_root / sources["reviewer_a"][1],
        review_b_path=output_root / sources["reviewer_b"][1],
        adjudication_path=output_root / sources["adjudicated"][1],
    )
    analysis["protocol_audit"] = protocol_audit
    analysis["artifacts"] = inventory
    analysis["build_checks"] = {
        "adjudication_covers_every_pair": True,
        "disagreement_packet_reproduced": True,
        "final_adjudication_reproduced": True,
        "human_gold_claim_is_false": True,
        "plan_reproduces_every_resolution": True,
        "review_assignments_and_ids_are_distinct": True,
    }
    report_path = output_root / "report.json"
    _write_json_object(report_path, analysis)
    return analysis


def validate_model_review_bundle(
    *,
    dataset_root: Path,
    calibration_root: Path,
    model_review_root: Path,
) -> dict[str, Any]:
    """Reproduce every derived artifact and compare the published report byte-for-byte."""

    expected_report = _load_json_object(model_review_root / "report.json")
    with tempfile.TemporaryDirectory(prefix="memoryos-model-review-validation-") as directory:
        reproduced = publish_model_review_bundle(
            dataset_root=dataset_root,
            calibration_root=calibration_root,
            review_a_path=model_review_root / "reviewer-a2.responses.jsonl",
            review_b_path=model_review_root / "reviewer-b.responses.jsonl",
            policy_path=model_review_root / "ADJUDICATION_POLICY.md",
            packet_path=model_review_root / "disagreement-packet.jsonl",
            plan_path=model_review_root / "decision-plan.json",
            resolutions_path=model_review_root / "resolutions.jsonl",
            adjudication_path=model_review_root / "adjudicated-provisional.jsonl",
            protocol_audit_path=model_review_root / "protocol-audit.json",
            output_root=Path(directory),
        )
    if reproduced != expected_report:
        raise ValueError("published model-review report does not reproduce from checked-in inputs")
    return {
        "schema_version": "1.0",
        "status": expected_report["status"],
        "human_gold_claim": expected_report["human_gold_claim"],
        "rows": expected_report["total_rows"],
        "core_disagreements_adjudicated": expected_report["core_disagreements_adjudicated"],
        "adjudication_sha256": expected_report["artifacts"]["adjudicated"]["sha256"],
        "all_build_checks_passed": all(expected_report["build_checks"].values()),
    }


def _verify_reproducible_chain(
    *,
    dataset_root: Path,
    review_a_path: Path,
    review_b_path: Path,
    packet_path: Path,
    plan_path: Path,
    resolutions_path: Path,
    adjudication_path: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="memoryos-model-review-") as directory:
        temporary = Path(directory)
        reproduced_packet = temporary / "packet.jsonl"
        prepare_model_adjudication(
            dataset_root=dataset_root,
            review_a_path=review_a_path,
            review_b_path=review_b_path,
            packet_path=reproduced_packet,
        )
        _require_same_bytes(reproduced_packet, packet_path, "disagreement packet")

        reproduced_resolutions = temporary / "resolutions.jsonl"
        draft_model_resolutions_from_plan(
            packet_path=packet_path,
            plan_path=plan_path,
            output_path=reproduced_resolutions,
        )
        _require_same_bytes(reproduced_resolutions, resolutions_path, "resolutions")

        reproduced_final = temporary / "adjudicated.jsonl"
        finalize_model_adjudication(
            dataset_root=dataset_root,
            review_a_path=review_a_path,
            review_b_path=review_b_path,
            resolutions_path=resolutions_path,
            output_path=reproduced_final,
        )
        _require_same_bytes(reproduced_final, adjudication_path, "final adjudication")


def _require_same_bytes(left: Path, right: Path, label: str) -> None:
    if left.read_bytes() != right.read_bytes():
        raise ValueError(f"{label} does not reproduce from its declared inputs")


def _canonical_text_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    if path.suffix.lower() in {".json", ".jsonl", ".md"}:
        payload = payload.replace(b"\r\n", b"\n")
    return payload


def _index_review(rows: Sequence[ReviewResponseDraft]) -> dict[Pair, ReviewResponseDraft]:
    indexed = {(row.case_id, row.candidate_id): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("review contains duplicate pairs")
    return indexed


def _index_adjudication(rows: Sequence[AdjudicatedReview]) -> dict[Pair, AdjudicatedReview]:
    indexed = {(row.case_id, row.candidate_id): row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("adjudication contains duplicate pairs")
    return indexed


def _complete_relevance(row: ReviewResponseDraft) -> int:
    if row.semantic_relevance is None:
        raise ValueError("review is incomplete")
    return row.semantic_relevance


def _complete_safety(row: ReviewResponseDraft) -> str:
    if row.safety_disposition is None:
        raise ValueError("review is incomplete")
    return row.safety_disposition.value


def _complete_required(row: ReviewResponseDraft) -> bool:
    if row.must_retrieve is None:
        raise ValueError("review is incomplete")
    return row.must_retrieve


def _complete_confidence(row: ReviewResponseDraft) -> str:
    if row.reviewer_confidence is None:
        raise ValueError("review is incomplete")
    return row.reviewer_confidence.value


def _core_decision(
    row: ReviewResponseDraft | AdjudicatedReview,
) -> tuple[int, str, bool]:
    if isinstance(row, ReviewResponseDraft):
        return _complete_relevance(row), _complete_safety(row), _complete_required(row)
    return row.semantic_relevance, row.safety_disposition.value, row.must_retrieve


def _cohen_kappa(
    left: Sequence[Label],
    right: Sequence[Label],
    *,
    categories: Sequence[Label],
) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("kappa requires equally sized non-empty label sequences")
    total = len(left)
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / total
    left_counts = Counter(left)
    right_counts = Counter(right)
    expected = sum(left_counts[item] * right_counts[item] for item in categories) / (total**2)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def _weighted_kappa(
    left: Sequence[Label],
    right: Sequence[Label],
    *,
    categories: Sequence[Label],
    quadratic: bool,
) -> float:
    if len(left) != len(right) or not left or len(categories) < 2:
        raise ValueError("weighted kappa requires matching labels and at least two categories")
    index = {value: position for position, value in enumerate(categories)}
    if len(index) != len(categories):
        raise ValueError("weighted kappa categories must be unique")
    maximum = len(categories) - 1

    def weight(a: Label, b: Label) -> float:
        distance = abs(index[a] - index[b]) / maximum
        return distance**2 if quadratic else distance

    total = len(left)
    observed_disagreement = sum(weight(a, b) for a, b in zip(left, right, strict=True)) / total
    left_counts = Counter(left)
    right_counts = Counter(right)
    expected_disagreement = sum(
        left_counts[a] * right_counts[b] * weight(a, b) for a in categories for b in categories
    ) / (total**2)
    if expected_disagreement == 0.0:
        return 1.0 if observed_disagreement == 0.0 else 0.0
    return 1.0 - observed_disagreement / expected_disagreement


def _review_distribution(rows: Sequence[ReviewResponseDraft]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "semantic_relevance": _counts(str(_complete_relevance(row)) for row in rows),
        "safety_disposition": _counts(_complete_safety(row) for row in rows),
        "must_retrieve": _counts(str(_complete_required(row)).lower() for row in rows),
        "reviewer_confidence": _counts(_complete_confidence(row) for row in rows),
    }


def _adjudication_distribution(rows: Sequence[AdjudicatedReview]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "semantic_relevance": _counts(str(row.semantic_relevance) for row in rows),
        "safety_disposition": _counts(row.safety_disposition.value for row in rows),
        "must_retrieve": _counts(str(row.must_retrieve).lower() for row in rows),
    }


def _reviewer_silver_metrics(
    review_index: dict[Pair, ReviewResponseDraft],
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, float]:
    values = [
        (_complete_relevance(review_index[pair]), judgment.relevance)
        for pair, _, judgment, _ in silver_pairs
    ]
    return _relevance_pair_metrics(values)


def _adjudicated_silver_metrics(
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, float]:
    values = [(row.semantic_relevance, judgment.relevance) for _, row, judgment, _ in silver_pairs]
    return _relevance_pair_metrics(values)


def _relevance_pair_metrics(values: Sequence[tuple[int, int]]) -> dict[str, float]:
    total = len(values)
    exact = sum(model == silver for model, silver in values) / total
    mean_absolute_error = sum(abs(model - silver) for model, silver in values) / total
    binary_agreement = sum((model >= 2) == (silver >= 2) for model, silver in values) / total
    return {
        "exact_relevance_rate": exact,
        "mean_absolute_error": mean_absolute_error,
        "binary_relevance_at_2_agreement": binary_agreement,
    }


def _relevance_confusion(
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, dict[str, int]]:
    matrix: dict[int, Counter[int]] = defaultdict(Counter)
    for _, row, judgment, _ in silver_pairs:
        matrix[judgment.relevance][row.semantic_relevance] += 1
    return {
        str(silver): {str(model): count for model, count in sorted(counts.items())}
        for silver, counts in sorted(matrix.items())
    }


def _safety_by_eligibility(
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, dict[str, int]]:
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for _, row, judgment, _ in silver_pairs:
        matrix[judgment.eligibility.value][row.safety_disposition.value] += 1
    return {
        eligibility: dict(sorted(counts.items())) for eligibility, counts in sorted(matrix.items())
    }


def _must_by_required(
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, dict[str, int]]:
    matrix: dict[bool, Counter[bool]] = defaultdict(Counter)
    for _, row, judgment, _ in silver_pairs:
        matrix[judgment.required][row.must_retrieve] += 1
    return {
        str(required).lower(): {
            str(model_required).lower(): count for model_required, count in sorted(counts.items())
        }
        for required, counts in sorted(matrix.items())
    }


def _silver_repository_slices(
    silver_pairs: Sequence[tuple[Pair, AdjudicatedReview, CalibrationJudgment, str]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for _, row, judgment, repository_id in silver_pairs:
        grouped[repository_id].append((row.semantic_relevance, judgment.relevance))
    return {
        repository_id: {
            "rows": len(values),
            **_relevance_pair_metrics(values),
        }
        for repository_id, values in sorted(grouped.items())
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON object: {path}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"JSON artifact must contain an object: {path}")
    return cast(dict[str, Any], decoded)


def _write_json_object(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _artifact_rows(relative_name: str, raw: bytes) -> int:
    if relative_name.endswith(".jsonl"):
        return len(raw.decode("utf-8").splitlines())
    return 1


__all__ = [
    "analyze_model_review",
    "publish_model_review_bundle",
    "validate_model_review_bundle",
]
