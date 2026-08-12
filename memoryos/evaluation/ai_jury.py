from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PresentationOrder(StrEnum):
    A_FIRST = "a_first"
    B_FIRST = "b_first"


class PairwisePreference(StrEnum):
    CANDIDATE_A = "candidate_a"
    CANDIDATE_B = "candidate_b"
    TIE = "tie"
    ABSTAIN = "abstain"


class JuryDecisionStatus(StrEnum):
    CONSENSUS = "consensus"
    UNCERTAIN = "uncertain"
    INVALID = "invalid"


class PairwiseJudgeVote(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    comparison_id: str = Field(min_length=1, max_length=160)
    query_id: str = Field(min_length=1, max_length=160)
    candidate_a_id: str = Field(min_length=1, max_length=160)
    candidate_b_id: str = Field(min_length=1, max_length=160)
    judge_id: str = Field(min_length=1, max_length=160)
    provider_id: str = Field(min_length=1, max_length=160)
    model_family: str = Field(min_length=1, max_length=160)
    judge_model: str = Field(min_length=1, max_length=300)
    model_revision: str = Field(min_length=1, max_length=300)
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    presentation_order: PresentationOrder
    preference: PairwisePreference
    confidence: float = Field(ge=0.0, le=1.0)
    rubric_version: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_comparison(self) -> PairwiseJudgeVote:
        if self.candidate_a_id == self.candidate_b_id:
            raise ValueError("pairwise candidates must be distinct")
        return self


class AIJuryProtocol(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    min_model_families: int = Field(default=3, ge=3, le=20)
    min_providers: int = Field(default=3, ge=3, le=20)
    smoothing_alpha: float = Field(default=0.25, gt=0.0, le=2.0)
    consensus_probability: float = Field(default=0.67, gt=0.5, le=1.0)
    max_normalized_entropy: float = Field(default=0.72, ge=0.0, le=1.0)
    min_swap_coverage: float = Field(default=0.9, ge=0.5, le=1.0)
    reliability_source: Literal["uniform_unverified", "executable_anchors"] = "uniform_unverified"
    family_reliability: dict[str, float] = Field(default_factory=dict)
    threshold_provenance: Literal["provisional_policy_default", "executable_anchor_calibrated"] = (
        "provisional_policy_default"
    )
    executable_anchor_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reliability(self) -> AIJuryProtocol:
        if any(not 0.05 <= value <= 1.0 for value in self.family_reliability.values()):
            raise ValueError("family reliability must stay within [0.05, 1.0]")
        if self.reliability_source == "uniform_unverified" and self.family_reliability:
            raise ValueError("unverified reliability must not contain learned family weights")
        calibrated = self.threshold_provenance == "executable_anchor_calibrated"
        if calibrated != (self.executable_anchor_sha256 is not None):
            raise ValueError("calibrated jury thresholds must name their executable anchors")
        if (
            self.reliability_source == "executable_anchors"
            and self.executable_anchor_sha256 is None
        ):
            raise ValueError("anchor-derived family reliability must name its executable anchors")
        return self


class AIJuryPairResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    comparison_id: str
    query_id: str
    candidate_a_id: str
    candidate_b_id: str
    status: JuryDecisionStatus
    decision: PairwisePreference
    model_families_seen: int = Field(ge=0)
    effective_model_families: int = Field(ge=0)
    providers_seen: int = Field(ge=0)
    effective_providers: int = Field(ge=0)
    judge_pairs_expected: int = Field(ge=0)
    judge_pairs_complete: int = Field(ge=0)
    swap_consistent_pairs: int = Field(ge=0)
    swap_inconsistent_pairs: int = Field(ge=0)
    abstained_pairs: int = Field(ge=0)
    swap_coverage: float = Field(ge=0.0, le=1.0)
    swap_consistency_rate: float = Field(ge=0.0, le=1.0)
    probability_a: float = Field(ge=0.0, le=1.0)
    probability_b: float = Field(ge=0.0, le=1.0)
    probability_tie: float = Field(ge=0.0, le=1.0)
    normalized_entropy: float = Field(ge=0.0, le=1.0)
    training_weight: float = Field(ge=0.0, le=1.0)
    reliability_source: Literal["uniform_unverified", "executable_anchors"]
    production_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_probabilities(self) -> AIJuryPairResult:
        total = self.probability_a + self.probability_b + self.probability_tie
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("jury probabilities must sum to one")
        if self.status is JuryDecisionStatus.INVALID and self.training_weight != 0.0:
            raise ValueError("invalid jury decisions must have zero training weight")
        return self


class CandidateUtility(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    query_id: str
    candidate_id: str
    utility: float
    rank: int = Field(ge=1)
    comparison_count: int = Field(ge=1)
    label_tier: Literal["ai_jury_provisional"] = "ai_jury_provisional"
    production_eligible: Literal[False] = False


def aggregate_ai_jury(
    votes: Sequence[PairwiseJudgeVote],
    *,
    protocol: AIJuryProtocol | None = None,
) -> list[AIJuryPairResult]:
    configured = protocol or AIJuryProtocol()
    grouped: dict[str, list[PairwiseJudgeVote]] = defaultdict(list)
    for vote in votes:
        grouped[vote.comparison_id].append(vote)
    return [
        _aggregate_comparison(grouped[comparison_id], configured)
        for comparison_id in sorted(grouped)
    ]


def rank_jury_candidates(
    results: Sequence[AIJuryPairResult],
    *,
    iterations: int = 1200,
    learning_rate: float = 0.35,
    l2: float = 0.08,
) -> list[CandidateUtility]:
    if iterations < 1 or learning_rate <= 0 or l2 < 0:
        raise ValueError("invalid Bradley-Terry optimizer settings")
    grouped: dict[str, list[AIJuryPairResult]] = defaultdict(list)
    for result in results:
        if result.status is not JuryDecisionStatus.INVALID and result.training_weight > 0:
            grouped[result.query_id].append(result)
    utilities: list[CandidateUtility] = []
    for query_id, query_results in sorted(grouped.items()):
        candidates = sorted(
            {
                candidate_id
                for result in query_results
                for candidate_id in (result.candidate_a_id, result.candidate_b_id)
            }
        )
        _require_connected(candidates, query_results)
        scores = {candidate_id: 0.0 for candidate_id in candidates}
        total_weight = sum(result.training_weight for result in query_results)
        for iteration in range(iterations):
            gradient = {candidate_id: -l2 * scores[candidate_id] for candidate_id in candidates}
            for result in query_results:
                target = result.probability_a + 0.5 * result.probability_tie
                difference = scores[result.candidate_a_id] - scores[result.candidate_b_id]
                predicted = _sigmoid(difference)
                error = result.training_weight * (target - predicted)
                gradient[result.candidate_a_id] += error
                gradient[result.candidate_b_id] -= error
            step = learning_rate / math.sqrt(1.0 + iteration / 100.0)
            denominator = max(total_weight, 1.0)
            for candidate_id in candidates:
                scores[candidate_id] += step * gradient[candidate_id] / denominator
            center = sum(scores.values()) / len(scores)
            for candidate_id in candidates:
                scores[candidate_id] -= center
        counts = Counter(
            candidate_id
            for result in query_results
            for candidate_id in (result.candidate_a_id, result.candidate_b_id)
        )
        ranked = sorted(candidates, key=lambda item: (-scores[item], item))
        utilities.extend(
            CandidateUtility(
                query_id=query_id,
                candidate_id=candidate_id,
                utility=round(scores[candidate_id], 10),
                rank=rank,
                comparison_count=counts[candidate_id],
            )
            for rank, candidate_id in enumerate(ranked, start=1)
        )
    return utilities


def _aggregate_comparison(
    votes: list[PairwiseJudgeVote],
    protocol: AIJuryProtocol,
) -> AIJuryPairResult:
    first = votes[0]
    identity = (
        first.query_id,
        first.candidate_a_id,
        first.candidate_b_id,
        first.rubric_version,
    )
    if any(
        (
            vote.query_id,
            vote.candidate_a_id,
            vote.candidate_b_id,
            vote.rubric_version,
        )
        != identity
        for vote in votes
    ):
        raise ValueError(f"comparison {first.comparison_id} mixes identities or rubrics")
    by_judge: dict[str, list[PairwiseJudgeVote]] = defaultdict(list)
    runtime_identities: dict[str, tuple[str, str, str, str]] = {}
    for vote in votes:
        by_judge[vote.judge_id].append(vote)
        identity = (
            vote.provider_id,
            vote.model_family,
            vote.judge_model,
            vote.model_revision,
        )
        previous = runtime_identities.setdefault(vote.runtime_sha256, identity)
        if previous != identity:
            raise ValueError("one judge runtime cannot claim multiple model identities")
    family_vectors: dict[str, list[tuple[float, float, float, float, str]]] = defaultdict(list)
    complete = 0
    consistent = 0
    inconsistent = 0
    abstained = 0
    for judge_id, judge_votes in by_judge.items():
        families = {vote.model_family for vote in judge_votes}
        models = {vote.judge_model for vote in judge_votes}
        providers = {vote.provider_id for vote in judge_votes}
        revisions = {vote.model_revision for vote in judge_votes}
        runtimes = {vote.runtime_sha256 for vote in judge_votes}
        if any(len(values) != 1 for values in (families, models, providers, revisions, runtimes)):
            raise ValueError(f"judge {judge_id} changes model identity inside a swap pair")
        orders = {vote.presentation_order for vote in judge_votes}
        if len(judge_votes) != 2 or orders != set(PresentationOrder):
            continue
        complete += 1
        left, right = sorted(judge_votes, key=lambda item: item.presentation_order.value)
        preference = _reconcile_swap(left.preference, right.preference)
        confidence = min(left.confidence, right.confidence)
        if preference is None:
            inconsistent += 1
            continue
        consistent += 1
        if preference is PairwisePreference.ABSTAIN:
            abstained += 1
            continue
        vector = {
            PairwisePreference.CANDIDATE_A: (1.0, 0.0, 0.0),
            PairwisePreference.CANDIDATE_B: (0.0, 1.0, 0.0),
            PairwisePreference.TIE: (0.0, 0.0, 1.0),
        }[preference]
        family_vectors[next(iter(families))].append((*vector, confidence, next(iter(providers))))

    family_totals: list[tuple[float, float, float, float, set[str]]] = []
    for family, vectors in sorted(family_vectors.items()):
        confidence_total = sum(max(vector[3], 0.05) for vector in vectors)
        averaged_a = sum(vector[0] * max(vector[3], 0.05) for vector in vectors) / confidence_total
        averaged_b = sum(vector[1] * max(vector[3], 0.05) for vector in vectors) / confidence_total
        averaged_tie = (
            sum(vector[2] * max(vector[3], 0.05) for vector in vectors) / confidence_total
        )
        reliability = protocol.family_reliability.get(family, 1.0)
        family_totals.append(
            (
                averaged_a,
                averaged_b,
                averaged_tie,
                reliability,
                {vector[4] for vector in vectors},
            )
        )

    alpha = protocol.smoothing_alpha
    a_score = alpha + sum(vector[0] * vector[3] for vector in family_totals)
    b_score = alpha + sum(vector[1] * vector[3] for vector in family_totals)
    tie_score = alpha + sum(vector[2] * vector[3] for vector in family_totals)
    total = a_score + b_score + tie_score
    probabilities = (a_score / total, b_score / total, tie_score / total)
    entropy = _normalized_entropy(probabilities)
    expected = len(by_judge)
    coverage = complete / expected if expected else 0.0
    consistency_rate = consistent / complete if complete else 0.0
    effective_families = len(family_totals)
    effective_providers = len({provider for vector in family_totals for provider in vector[4]})
    invalid = (
        effective_families < protocol.min_model_families
        or effective_providers < protocol.min_providers
        or coverage < protocol.min_swap_coverage
    )
    maximum_probability = max(probabilities)
    if invalid:
        status = JuryDecisionStatus.INVALID
        decision = PairwisePreference.ABSTAIN
        training_weight = 0.0
    else:
        maximum_index = probabilities.index(maximum_probability)
        proposed = (
            PairwisePreference.CANDIDATE_A,
            PairwisePreference.CANDIDATE_B,
            PairwisePreference.TIE,
        )[maximum_index]
        if (
            maximum_probability >= protocol.consensus_probability
            and entropy <= protocol.max_normalized_entropy
        ):
            status = JuryDecisionStatus.CONSENSUS
            decision = proposed
        else:
            status = JuryDecisionStatus.UNCERTAIN
            decision = PairwisePreference.ABSTAIN
        abstention_rate = (abstained + inconsistent + expected - complete) / expected
        diversity_factor = min(1.0, effective_families / protocol.min_model_families)
        training_weight = max(
            0.0,
            min(1.0, (1.0 - entropy) * (1.0 - abstention_rate) * diversity_factor),
        )
    return AIJuryPairResult(
        comparison_id=first.comparison_id,
        query_id=first.query_id,
        candidate_a_id=first.candidate_a_id,
        candidate_b_id=first.candidate_b_id,
        status=status,
        decision=decision,
        model_families_seen=len({vote.model_family for vote in votes}),
        effective_model_families=effective_families,
        providers_seen=len({vote.provider_id for vote in votes}),
        effective_providers=effective_providers,
        judge_pairs_expected=expected,
        judge_pairs_complete=complete,
        swap_consistent_pairs=consistent,
        swap_inconsistent_pairs=inconsistent,
        abstained_pairs=abstained,
        swap_coverage=coverage,
        swap_consistency_rate=consistency_rate,
        probability_a=probabilities[0],
        probability_b=probabilities[1],
        probability_tie=probabilities[2],
        normalized_entropy=entropy,
        training_weight=training_weight,
        reliability_source=protocol.reliability_source,
    )


def _reconcile_swap(
    first: PairwisePreference,
    second: PairwisePreference,
) -> PairwisePreference | None:
    if first is PairwisePreference.ABSTAIN or second is PairwisePreference.ABSTAIN:
        return PairwisePreference.ABSTAIN
    return first if first is second else None


def _normalized_entropy(probabilities: tuple[float, float, float]) -> float:
    entropy = -sum(value * math.log(value) for value in probabilities if value > 0)
    return entropy / math.log(len(probabilities))


def _require_connected(
    candidates: list[str],
    results: list[AIJuryPairResult],
) -> None:
    adjacency: dict[str, set[str]] = {candidate_id: set() for candidate_id in candidates}
    for result in results:
        adjacency[result.candidate_a_id].add(result.candidate_b_id)
        adjacency[result.candidate_b_id].add(result.candidate_a_id)
    seen = {candidates[0]}
    frontier = [candidates[0]]
    while frontier:
        current = frontier.pop()
        for neighbor in adjacency[current] - seen:
            seen.add(neighbor)
            frontier.append(neighbor)
    if seen != set(candidates):
        raise ValueError("Bradley-Terry comparisons must connect every candidate within a query")


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-value)
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


__all__ = [
    "AIJuryPairResult",
    "AIJuryProtocol",
    "CandidateUtility",
    "JuryDecisionStatus",
    "PairwiseJudgeVote",
    "PairwisePreference",
    "PresentationOrder",
    "aggregate_ai_jury",
    "rank_jury_candidates",
]
