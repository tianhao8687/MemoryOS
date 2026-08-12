from __future__ import annotations

import hashlib

import pytest

from memoryos.evaluation.ai_jury import (
    AIJuryProtocol,
    JuryDecisionStatus,
    PairwiseJudgeVote,
    PairwisePreference,
    PresentationOrder,
    aggregate_ai_jury,
    rank_jury_candidates,
)


def _swap_votes(
    comparison_id: str,
    candidate_a: str,
    candidate_b: str,
    *,
    judge_id: str,
    family: str,
    preference: PairwisePreference,
    reverse_preference: PairwisePreference | None = None,
) -> list[PairwiseJudgeVote]:
    return [
        PairwiseJudgeVote(
            comparison_id=comparison_id,
            query_id="query-1",
            candidate_a_id=candidate_a,
            candidate_b_id=candidate_b,
            judge_id=judge_id,
            provider_id=family.replace("family", "provider"),
            model_family=family,
            judge_model=f"{family}-judge",
            model_revision=f"{family}-revision-20260812",
            runtime_sha256=hashlib.sha256(f"runtime:{family}:{judge_id}".encode()).hexdigest(),
            prompt_sha256=hashlib.sha256(
                f"prompt:{comparison_id}:{judge_id}:{order.value}".encode()
            ).hexdigest(),
            response_sha256=hashlib.sha256(
                f"response:{comparison_id}:{judge_id}:{order.value}".encode()
            ).hexdigest(),
            presentation_order=order,
            preference=(
                preference
                if order is PresentationOrder.A_FIRST
                else reverse_preference or preference
            ),
            confidence=0.9,
            rubric_version="utility-v1",
            rationale="The preferred candidate contributes the more actionable constraint.",
        )
        for order in PresentationOrder
    ]


def test_ai_jury_requires_family_diversity_and_swap_consistency() -> None:
    votes = []
    for index, family in enumerate(("family-a", "family-b", "family-c"), start=1):
        votes.extend(
            _swap_votes(
                "comparison-1",
                "candidate-a",
                "candidate-b",
                judge_id=f"judge-{index}",
                family=family,
                preference=PairwisePreference.CANDIDATE_A,
            )
        )

    result = aggregate_ai_jury(votes)[0]

    assert result.status is JuryDecisionStatus.CONSENSUS
    assert result.decision is PairwisePreference.CANDIDATE_A
    assert result.effective_model_families == 3
    assert result.effective_providers == 3
    assert result.swap_consistency_rate == 1.0
    assert result.probability_a > 0.8
    assert 0 < result.training_weight < 1
    assert result.production_eligible is False

    same_family = [vote.model_copy(update={"model_family": "one-family"}) for vote in votes]
    invalid = aggregate_ai_jury(same_family)[0]
    assert invalid.status is JuryDecisionStatus.INVALID
    assert invalid.training_weight == 0.0

    one_provider = [vote.model_copy(update={"provider_id": "one-provider"}) for vote in votes]
    provider_invalid = aggregate_ai_jury(one_provider)[0]
    assert provider_invalid.status is JuryDecisionStatus.INVALID
    assert provider_invalid.effective_providers == 1

    shared_runtime = votes[0].runtime_sha256
    spoofed_runtime = [
        vote.model_copy(update={"runtime_sha256": shared_runtime})
        if vote.model_family == "family-c"
        else vote
        for vote in votes
    ]
    with pytest.raises(ValueError, match="one judge runtime"):
        aggregate_ai_jury(spoofed_runtime)


def test_ai_jury_abstains_when_swapped_order_changes_the_judgment() -> None:
    votes = []
    votes.extend(
        _swap_votes(
            "comparison-1",
            "candidate-a",
            "candidate-b",
            judge_id="unstable",
            family="family-a",
            preference=PairwisePreference.CANDIDATE_A,
            reverse_preference=PairwisePreference.CANDIDATE_B,
        )
    )
    for index, family in enumerate(("family-b", "family-c"), start=1):
        votes.extend(
            _swap_votes(
                "comparison-1",
                "candidate-a",
                "candidate-b",
                judge_id=f"stable-{index}",
                family=family,
                preference=PairwisePreference.CANDIDATE_A,
            )
        )

    result = aggregate_ai_jury(
        votes,
        protocol=AIJuryProtocol(min_model_families=3),
    )[0]

    assert result.status is JuryDecisionStatus.INVALID
    assert result.swap_inconsistent_pairs == 1
    assert result.effective_model_families == 2


def test_bradley_terry_ranking_uses_probabilistic_jury_results() -> None:
    votes = []
    for comparison, left, right in (
        ("a-over-b", "candidate-a", "candidate-b"),
        ("b-over-c", "candidate-b", "candidate-c"),
        ("a-over-c", "candidate-a", "candidate-c"),
    ):
        for index, family in enumerate(("family-a", "family-b", "family-c"), start=1):
            votes.extend(
                _swap_votes(
                    comparison,
                    left,
                    right,
                    judge_id=f"{comparison}-judge-{index}",
                    family=family,
                    preference=PairwisePreference.CANDIDATE_A,
                )
            )

    utilities = rank_jury_candidates(aggregate_ai_jury(votes))

    assert [item.candidate_id for item in utilities] == [
        "candidate-a",
        "candidate-b",
        "candidate-c",
    ]
    assert utilities[0].utility > utilities[1].utility > utilities[2].utility


def test_bradley_terry_rejects_disconnected_tournaments() -> None:
    votes = []
    for index, family in enumerate(("family-a", "family-b", "family-c"), start=1):
        votes.extend(
            _swap_votes(
                "a-over-b",
                "candidate-a",
                "candidate-b",
                judge_id=f"ab-{index}",
                family=family,
                preference=PairwisePreference.CANDIDATE_A,
            )
        )
        votes.extend(
            _swap_votes(
                "c-over-d",
                "candidate-c",
                "candidate-d",
                judge_id=f"cd-{index}",
                family=family,
                preference=PairwisePreference.CANDIDATE_A,
            )
        )

    with pytest.raises(ValueError, match="connect every candidate"):
        rank_jury_candidates(aggregate_ai_jury(votes))
