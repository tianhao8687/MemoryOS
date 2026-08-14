from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from memoryos.config import MemoryOSSettings
from memoryos.context.atoms import CompressionPolicy, ContextAtom
from memoryos.context.token_meter import TokenCounter, canonical_json
from memoryos.domain.schemas import BudgetProfile, ContextRequest, QueryIntent, TokenCounterKind
from memoryos.errors import InsufficientBudgetError, TokenizerUnavailableError

BUDGET_POLICY_VERSION = "msc-budget-v1"
AUTO_COVERAGE_RAISE_THRESHOLD = 3

AUTO_START: dict[QueryIntent, BudgetProfile] = {
    QueryIntent.PREFERENCE: BudgetProfile.TINY,
    QueryIntent.IMPLEMENTATION_LOCATION: BudgetProfile.TINY,
    QueryIntent.CURRENT_DECISION: BudgetProfile.SMALL,
    QueryIntent.CONSTRAINT_LOOKUP: BudgetProfile.SMALL,
    QueryIntent.TASK_STATE: BudgetProfile.SMALL,
    QueryIntent.WHY_DECISION: BudgetProfile.MEDIUM,
    QueryIntent.FAILURE_HISTORY: BudgetProfile.MEDIUM,
    QueryIntent.HISTORICAL_AS_OF: BudgetProfile.MEDIUM,
    QueryIntent.BROAD_SEARCH: BudgetProfile.MEDIUM,
}
PROFILE_ORDER = (
    BudgetProfile.TINY,
    BudgetProfile.SMALL,
    BudgetProfile.MEDIUM,
    BudgetProfile.LARGE,
)


class AtomBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    bundle_id: str
    atoms: tuple[ContextAtom, ...]
    required: bool
    safety_required: bool
    reasons: tuple[str, ...]
    utility: float


class BudgetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    requested_tokens: int | None
    effective_tokens: int = Field(ge=1)
    minimum_safe_tokens: int = Field(default=0, ge=0)
    floor_raised: bool = False
    reasons: tuple[str, ...]
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BudgetPlanner:
    def __init__(self, settings: MemoryOSSettings, counter: TokenCounter) -> None:
        self.settings = settings
        self.counter = counter
        self.limits = {
            BudgetProfile.TINY: settings.context_budget_tiny_tokens,
            BudgetProfile.SMALL: settings.context_budget_small_tokens,
            BudgetProfile.MEDIUM: settings.context_budget_medium_tokens,
            BudgetProfile.LARGE: settings.context_budget_large_tokens,
        }
        self.policy_hash = hashlib.sha256(
            canonical_json(
                {
                    "version": BUDGET_POLICY_VERSION,
                    "profiles": {key.value: value for key, value in self.limits.items()},
                    "auto_start": {key.value: value.value for key, value in AUTO_START.items()},
                    "delta_fallback_ratio": settings.context_delta_fallback_ratio,
                    "snapshot_ttl_seconds": settings.context_snapshot_ttl_seconds,
                }
            ).encode("utf-8")
        ).hexdigest()

    def plan(
        self,
        request: ContextRequest,
        intent: QueryIntent,
        atoms: list[ContextAtom],
        *,
        coverage_count: int,
    ) -> BudgetDecision:
        if request.hard_token_budget and self.counter.kind is not TokenCounterKind.EXACT:
            raise TokenizerUnavailableError(
                "hard_token_budget requires an exact counter for the selected tokenizer",
                details={
                    "counter_kind": self.counter.kind.value,
                    "tokenizer_id": self.counter.tokenizer_id,
                },
            )
        reasons: list[str] = []
        if request.budget_tokens is not None:
            profile = "custom"
            requested = request.budget_tokens
            effective = request.budget_tokens
            reasons.append("explicit_budget_tokens")
        elif request.budget_profile is not BudgetProfile.AUTO:
            profile = request.budget_profile.value
            requested = None
            effective = self.limits[request.budget_profile]
            reasons.append(f"profile={profile}")
        else:
            selected = AUTO_START[intent]
            reasons.extend((f"intent={intent.value}", f"coverage={coverage_count}"))
            should_raise = (
                any(atom.truth_state.value == "contested" for atom in atoms)
                or coverage_count >= AUTO_COVERAGE_RAISE_THRESHOLD
                or any(
                    atom.compression_policy is CompressionPolicy.SOURCE_GROUNDED for atom in atoms
                )
            )
            if should_raise:
                selected = _next_profile(selected)
                reasons.append("candidate_structure_raise")
            profile = selected.value
            requested = None
            effective = self.limits[selected]
        return BudgetDecision(
            profile=profile,
            requested_tokens=requested,
            effective_tokens=effective,
            reasons=tuple(reasons),
            policy_hash=self.policy_hash,
        )

    @staticmethod
    def apply_safe_floor(
        decision: BudgetDecision,
        request: ContextRequest,
        minimum_safe_tokens: int,
    ) -> BudgetDecision:
        if minimum_safe_tokens <= decision.effective_tokens:
            return decision.model_copy(update={"minimum_safe_tokens": minimum_safe_tokens})
        if request.hard_token_budget:
            raise InsufficientBudgetError(
                "the hard token budget cannot contain the complete required context bundle",
                details={
                    "effective_tokens": decision.effective_tokens,
                    "minimum_safe_tokens": minimum_safe_tokens,
                    "profile": decision.profile,
                },
            )
        return decision.model_copy(
            update={
                "effective_tokens": minimum_safe_tokens,
                "minimum_safe_tokens": minimum_safe_tokens,
                "floor_raised": True,
                "reasons": (*decision.reasons, "minimum_safe_floor"),
            }
        )


def build_bundles(
    atoms: list[ContextAtom],
    coverage_categories: list[str],
) -> list[AtomBundle]:
    contested: dict[str, list[ContextAtom]] = {}
    singles: list[list[ContextAtom]] = []
    for atom in atoms:
        if atom.truth_state.value == "contested":
            contested.setdefault(atom.bundle_key, []).append(atom)
        else:
            singles.append([atom])
    groups = [*contested.values(), *singles]
    coverage_winners: dict[str, str] = {}
    for category in coverage_categories:
        candidates = [group for group in groups if any(atom.category == category for atom in group)]
        if candidates:
            winner_group = max(candidates, key=lambda group: max(atom.utility for atom in group))
            coverage_winners[category] = _bundle_id(winner_group)

    result: list[AtomBundle] = []
    for group in groups:
        ordered = tuple(sorted(group, key=lambda item: (item.memory_id, item.atom_sha256)))
        identity = _bundle_id(group)
        reasons: list[str] = []
        safety_required = any(
            atom.compression_policy is CompressionPolicy.PINNED
            or atom.truth_state.value == "contested"
            for atom in ordered
        )
        if any(atom.compression_policy is CompressionPolicy.PINNED for atom in ordered):
            reasons.append("pinned")
        if any(atom.truth_state.value == "contested" for atom in ordered):
            reasons.append("contested_complete")
        for category, winner_id in coverage_winners.items():
            if winner_id == identity:
                reasons.append(f"coverage={category}")
        required = safety_required or any(reason.startswith("coverage=") for reason in reasons)
        result.append(
            AtomBundle(
                bundle_id=identity,
                atoms=ordered,
                required=required,
                safety_required=safety_required,
                reasons=tuple(reasons),
                utility=sum(atom.utility for atom in ordered),
            )
        )
    result.sort(
        key=lambda bundle: (
            not bundle.required,
            -(bundle.utility / max(1, sum(atom.estimated_tokens for atom in bundle.atoms))),
            bundle.bundle_id,
        )
    )
    return result


def budget_manifest(decision: BudgetDecision) -> dict[str, Any]:
    return decision.model_dump(mode="json")


def _next_profile(value: BudgetProfile) -> BudgetProfile:
    index = PROFILE_ORDER.index(value)
    return PROFILE_ORDER[min(index + 1, len(PROFILE_ORDER) - 1)]


def _bundle_id(atoms: list[ContextAtom] | tuple[ContextAtom, ...]) -> str:
    return hashlib.sha256(
        canonical_json(sorted(atom.atom_sha256 for atom in atoms)).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AUTO_COVERAGE_RAISE_THRESHOLD",
    "BUDGET_POLICY_VERSION",
    "AtomBundle",
    "BudgetDecision",
    "BudgetPlanner",
    "budget_manifest",
    "build_bundles",
]
