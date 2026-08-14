from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ScopeType(StrEnum):
    USER = "user"
    WORKSPACE = "workspace"
    REPOSITORY = "repository"
    BRANCH = "branch"
    TASK = "task"


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    PROJECT = "project"
    WORKING = "working"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"
    REJECTED = "rejected"


class SourceType(StrEnum):
    CONVERSATION = "conversation"
    MANUAL = "manual"
    GIT_COMMIT = "git_commit"
    FILE_REFERENCE = "file_reference"
    AGENT = "agent"
    IMPORT = "import"


class CreatedBy(StrEnum):
    MANUAL = "manual"
    AGENT = "agent"
    EXTRACTOR = "extractor"
    IMPORT = "import"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"


class ConflictStrategy(StrEnum):
    SUPERSEDE = "supersede"
    KEEP_BOTH = "keep_both"
    REJECT = "reject"


class EntityType(StrEnum):
    PROJECT = "project"
    REPOSITORY = "repository"
    FILE = "file"
    SYMBOL = "symbol"
    DEPENDENCY = "dependency"
    SERVICE = "service"
    DATABASE = "database"
    TOOL = "tool"
    PERSON = "person"
    CONCEPT = "concept"
    OTHER = "other"


class ClaimObjectKind(StrEnum):
    LITERAL = "literal"
    ENTITY = "entity"
    JSON = "json"


class ClaimPolarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class ClaimModality(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    PREFERENCE = "preference"
    OBSERVATION = "observation"
    FAILURE = "failure"


class ClaimStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    STALE = "stale"
    HISTORICAL = "historical"
    REJECTED = "rejected"


class ClaimStaleState(StrEnum):
    FRESH = "fresh"
    SUSPECT = "suspect"
    STALE = "stale"
    UNKNOWN = "unknown"


class FreshnessState(StrEnum):
    FRESH = "fresh"
    MOVED = "moved"
    SUSPECT = "suspect"
    STALE = "stale"
    UNKNOWN = "unknown"


class ClaimRelationType(StrEnum):
    EQUIVALENT_TO = "equivalent_to"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DEPENDS_ON = "depends_on"
    DERIVED_FROM = "derived_from"
    ALTERNATIVE_TO = "alternative_to"
    CONSOLIDATED_FROM = "consolidated_from"


class RelationMethod(StrEnum):
    EXACT = "exact"
    RULE = "rule"
    EMBEDDING = "embedding"
    MODEL_JUDGE = "model_judge"
    MANUAL = "manual"


class TruthState(StrEnum):
    RESOLVED = "resolved"
    CONTESTED = "contested"
    STALE = "stale"
    UNKNOWN = "unknown"


class FeedbackValue(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class MemoryTemperature(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    ARCHIVED = "archived"


class PossibleConflictStatus(StrEnum):
    POSSIBLE = "possible"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    ABSTAINED = "abstained"


class QueryIntent(StrEnum):
    CURRENT_DECISION = "current_decision"
    CONSTRAINT_LOOKUP = "constraint_lookup"
    FAILURE_HISTORY = "failure_history"
    WHY_DECISION = "why_decision"
    IMPLEMENTATION_LOCATION = "implementation_location"
    PREFERENCE = "preference"
    TASK_STATE = "task_state"
    HISTORICAL_AS_OF = "historical_as_of"
    BROAD_SEARCH = "broad_search"


class DetailLevel(StrEnum):
    INDEX = "index"
    FACT = "fact"
    EVIDENCE = "evidence"
    HISTORY = "history"


class BudgetProfile(StrEnum):
    AUTO = "auto"
    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TokenCounterKind(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"


class MemoryOperationTokenAttribution(StrEnum):
    EXACT_ZERO = "exact_zero"
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class ExplainSection(StrEnum):
    FACT = "fact"
    EVIDENCE = "evidence"
    FRESHNESS = "freshness"
    RELATIONS = "relations"
    HISTORY = "history"


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def validate_order(self) -> EvidenceSpan:
        if self.end <= self.start:
            raise ValueError("evidence span end must be later than start")
        return self


class ClaimCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_hint: str = Field(min_length=1, max_length=500)
    subject_type: EntityType = EntityType.CONCEPT
    predicate: str = Field(min_length=1, max_length=120)
    object_kind: ClaimObjectKind = ClaimObjectKind.LITERAL
    object_value: Any = None
    object_entity_hint: str | None = Field(default=None, max_length=500)
    object_entity_type: EntityType | None = None
    polarity: ClaimPolarity = ClaimPolarity.POSITIVE
    modality: ClaimModality = ClaimModality.FACT
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.7, ge=0, le=1)
    evidence_span: EvidenceSpan

    @model_validator(mode="after")
    def validate_object(self) -> ClaimCandidate:
        if self.object_kind is ClaimObjectKind.ENTITY and not self.object_entity_hint:
            raise ValueError("entity objects require object_entity_hint")
        if self.object_kind is not ClaimObjectKind.ENTITY and self.object_value is None:
            raise ValueError("literal/json objects require object_value")
        return self


class EntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: ScopeType
    scope_key: str = Field(min_length=1, max_length=1000)
    entity_type: EntityType
    canonical_name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    stable_external_key: str | None = Field(default=None, max_length=1000)


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_ref: str = Field(min_length=1, max_length=1000)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    excerpt: str = Field(min_length=1, max_length=10000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: ScopeType
    scope_key: str = Field(min_length=1, max_length=1000)
    memory_type: MemoryType
    category: str = Field(min_length=1, max_length=120)
    subject: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    confidence: float = Field(default=0.8, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ttl_seconds: int | None = Field(default=None, gt=0, le=315_360_000)
    created_by: CreatedBy = CreatedBy.AGENT
    sensitivity: Sensitivity = Sensitivity.NORMAL
    metadata: dict[str, Any] = Field(default_factory=dict)
    claim_candidates: list[ClaimCandidate] = Field(default_factory=list, max_length=50)
    source: SourceCreate
    activate_immediately: bool = False

    @model_validator(mode="after")
    def validate_activation_and_validity(self) -> MemoryCreate:
        if self.activate_immediately and self.created_by is not CreatedBy.MANUAL:
            raise ValueError("only manual writes may activate immediately")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        if self.scope_type is ScopeType.TASK and self.ttl_seconds is None:
            self.ttl_seconds = 604_800
        return self


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    subject: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=300)
    confidence: float | None = Field(default=None, ge=0, le=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    ttl_seconds: int | None = Field(default=None, gt=0, le=315_360_000)
    sensitivity: Sensitivity | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_validity(self) -> MemoryUpdate:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=1000)
    scope_type: ScopeType | None = None
    scope_key: str | None = None
    memory_type: MemoryType | None = None
    status: MemoryStatus | None = None
    include_history: bool = False
    as_of_valid_time: datetime | None = None
    as_known_at: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=500)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=2000)
    repository: str
    branch: str | None = None
    workspace: str | None = None
    task_scope: str | None = None
    budget: int = Field(default=6000, ge=500, le=50000)
    budget_tokens: int | None = Field(default=None, ge=1, le=50000)
    budget_profile: BudgetProfile = BudgetProfile.AUTO
    tokenizer_id: str | None = Field(default=None, min_length=1, max_length=300)
    hard_token_budget: bool = False
    detail_level: DetailLevel = DetailLevel.FACT
    previous_context_id: str | None = Field(default=None, min_length=1, max_length=64)
    response_mode: Literal["auto", "full", "delta"] = "auto"
    include_historical: bool = False
    as_of_valid_time: datetime | None = None
    as_known_at: datetime | None = None

    @field_validator("repository")
    @classmethod
    def require_repository(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("repository is required")
        return value.strip()

    @model_validator(mode="after")
    def validate_token_budget_choice(self) -> ContextRequest:
        if self.budget_tokens is not None and self.budget_profile is not BudgetProfile.AUTO:
            raise ValueError("budget_tokens and a non-auto budget_profile are mutually exclusive")
        if self.detail_level in {DetailLevel.EVIDENCE, DetailLevel.HISTORY}:
            raise ValueError(
                "memory_context supports index/fact detail; use memory_explain for "
                "evidence/history expansion"
            )
        return self


class ContextUsage(BaseModel):
    """Server-side context attribution; provider Usage remains authoritative end to end."""

    model_config = ConfigDict(extra="forbid")

    counter_kind: TokenCounterKind
    tokenizer_id: str
    counter_version: str
    full_context_tokens: int = Field(ge=0)
    context_text_tokens: int = Field(ge=0)
    payload_overhead_tokens: int = Field(ge=0)
    delivered_payload_tokens: int = Field(ge=0)
    delta_tokens: int = Field(default=0, ge=0)
    evidence_expansion_tokens: int = Field(default=0, ge=0)
    history_expansion_tokens: int = Field(default=0, ge=0)
    legacy_equivalent_tokens: int = Field(default=0, ge=0)
    selection_latency_ms: float = Field(default=0.0, ge=0)
    render_latency_ms: float = Field(default=0.0, ge=0)
    context_compilation_llm_input_tokens: int = Field(default=0, ge=0)
    context_compilation_llm_output_tokens: int = Field(default=0, ge=0)
    other_memory_operation_llm_input_tokens: int | None = Field(default=0, ge=0)
    other_memory_operation_llm_output_tokens: int | None = Field(default=0, ge=0)
    other_memory_operation_token_attribution: MemoryOperationTokenAttribution = Field(
        default_factory=lambda: MemoryOperationTokenAttribution.EXACT_ZERO
    )

    @model_validator(mode="after")
    def validate_other_memory_operation_attribution(self) -> ContextUsage:
        values = (
            self.other_memory_operation_llm_input_tokens,
            self.other_memory_operation_llm_output_tokens,
        )
        if (
            self.other_memory_operation_token_attribution
            is MemoryOperationTokenAttribution.EXACT_ZERO
            and values != (0, 0)
        ):
            raise ValueError("exact_zero other-memory attribution requires zero token values")
        if self.other_memory_operation_token_attribution in {
            MemoryOperationTokenAttribution.EXACT,
            MemoryOperationTokenAttribution.ESTIMATED,
        } and any(value is None for value in values):
            raise ValueError("attributed other-memory operations require complete token values")
        if (
            self.other_memory_operation_token_attribution
            is MemoryOperationTokenAttribution.UNAVAILABLE
            and any(value is not None for value in values)
        ):
            raise ValueError("unavailable other-memory attribution cannot carry token values")
        return self


class MSCContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.3"] = "2.3"
    mode: Literal["full", "delta"]
    context_id: str
    requires_base_context_id: str | None = None
    retrieval_run_id: str
    truth_state: str
    text: str
    delta: dict[str, Any] | None = None
    usage: ContextUsage
    fallback_reason: str | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_mode_shape(self) -> MSCContextResponse:
        if self.mode == "full":
            if self.requires_base_context_id is not None or self.delta is not None:
                raise ValueError("full context cannot require a base or contain a delta summary")
        elif self.requires_base_context_id is None or self.delta is None:
            raise ValueError("delta context requires a base context and delta summary")
        return self


class MemoryView(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    scope_type: ScopeType
    scope_key: str
    memory_type: MemoryType
    category: str
    subject: str | None
    key: str | None
    title: str
    content: str
    status: MemoryStatus
    confidence: float
    importance: float
    valid_from: datetime | None
    valid_to: datetime | None
    ttl_seconds: int | None
    supersedes_id: str | None
    created_at: datetime
    updated_at: datetime
    created_by: CreatedBy
    sensitivity: Sensitivity
    metadata_json: dict[str, Any] = Field(alias="metadata")


class ProviderCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    memory_type: MemoryType
    category: str = Field(min_length=1, max_length=120)
    subject: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=300)
    confidence: float = Field(default=0.7, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    ttl_seconds: int | None = Field(default=None, gt=0)
    claim_candidates: list[ClaimCandidate] = Field(default_factory=list, max_length=50)


class CurrentTruthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: ScopeType | None = None
    scope_key: str | None = Field(default=None, max_length=1000)
    subject: str | None = Field(default=None, max_length=500)
    predicate: str | None = Field(default=None, max_length=120)
    query: str | None = Field(default=None, max_length=1000)
    as_of_valid_time: datetime | None = None
    as_known_at: datetime | None = None

    @model_validator(mode="after")
    def require_selector(self) -> CurrentTruthRequest:
        if not any((self.subject, self.predicate, self.query)):
            raise ValueError("subject, predicate, or query is required")
        return self


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_run_id: str = Field(min_length=1, max_length=36)
    memory_id: str = Field(min_length=1, max_length=36)
    helpful: FeedbackValue
    actor: str = Field(default="agent", min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)


class ConsolidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: ScopeType
    scope_key: str = Field(min_length=1, max_length=1000)
    dry_run: bool = True
    minimum_sources: int = Field(default=3, ge=2, le=20)
    minimum_span_days: int = Field(default=7, ge=0, le=3650)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=36)
    repository_path: str = Field(min_length=1, max_length=2000)
    create_replacement_candidate: bool = False
