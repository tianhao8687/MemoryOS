from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from memoryos.db.base import Base
from memoryos.domain.schemas import (
    ClaimModality,
    ClaimObjectKind,
    ClaimPolarity,
    ClaimRelationType,
    ClaimStaleState,
    ClaimStatus,
    CreatedBy,
    EntityType,
    FeedbackValue,
    FreshnessState,
    MemoryStatus,
    MemoryType,
    RelationMethod,
    ScopeType,
    Sensitivity,
    SourceType,
)


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_type: type[Any], length: int = 32) -> Enum:
    return Enum(
        enum_type,
        values_callable=lambda members: [member.value for member in members],
        native_enum=False,
        create_constraint=True,
        length=length,
    )


class RepositoryRow(Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stable_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str] = mapped_column(String(2000), nullable=False)
    remote_url: Mapped[str | None] = mapped_column(String(2000))
    default_branch: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class MemoryRow(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_scope_status", "scope_type", "scope_key", "status"),
        Index("ix_memories_semantic_key", "scope_type", "scope_key", "key", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[ScopeType] = mapped_column(enum_column(ScopeType), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    memory_type: Mapped[MemoryType] = mapped_column(enum_column(MemoryType), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(300))
    key: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MemoryStatus] = mapped_column(
        enum_column(MemoryStatus), nullable=False, default=MemoryStatus.CANDIDATE
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ttl_seconds: Mapped[int | None] = mapped_column(Integer)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    created_by: Mapped[CreatedBy] = mapped_column(enum_column(CreatedBy), nullable=False)
    sensitivity: Mapped[Sensitivity] = mapped_column(
        enum_column(Sensitivity), nullable=False, default=Sensitivity.NORMAL
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    sources: Mapped[list[SourceRow]] = relationship(
        secondary="memory_sources", back_populates="memories", lazy="selectin"
    )


class SourceRow(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[SourceType] = mapped_column(enum_column(SourceType), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(1000), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    memories: Mapped[list[MemoryRow]] = relationship(
        secondary="memory_sources", back_populates="sources"
    )


class MemorySourceRow(Base):
    __tablename__ = "memory_sources"

    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )


class RelationRow(Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("from_memory_id", "to_memory_id", "relation_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    to_memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EmbeddingRow(Base):
    __tablename__ = "embeddings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(300), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    vector_json: Mapped[list[float] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class EntityRow(Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_key",
            "entity_type",
            "normalized_name",
            name="uq_entities_scoped_name",
        ),
        Index("ix_entities_scope_type", "scope_type", "scope_key", "entity_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[ScopeType] = mapped_column(enum_column(ScopeType), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(enum_column(EntityType), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(500), nullable=False)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    stable_external_key: Mapped[str | None] = mapped_column(String(1000), index=True)
    redirect_to_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class EntityMergeEventRow(Base):
    __tablename__ = "entity_merge_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    to_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClaimRow(Base):
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_subject_predicate_status", "subject_entity_id", "predicate", "status"),
        Index("ix_claims_memory_status", "memory_id", "status"),
        Index("ix_claims_temporal", "valid_from", "valid_to", "recorded_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    subject_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(120), nullable=False)
    object_kind: Mapped[ClaimObjectKind] = mapped_column(
        enum_column(ClaimObjectKind), nullable=False
    )
    object_entity_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="RESTRICT")
    )
    object_value: Mapped[Any | None] = mapped_column(JSON)
    polarity: Mapped[ClaimPolarity] = mapped_column(enum_column(ClaimPolarity), nullable=False)
    modality: Mapped[ClaimModality] = mapped_column(enum_column(ClaimModality), nullable=False)
    qualifiers_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    canonical_key: Mapped[str] = mapped_column(String(1200), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    status: Mapped[ClaimStatus] = mapped_column(
        enum_column(ClaimStatus), nullable=False, default=ClaimStatus.CANDIDATE
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    stale_state: Mapped[ClaimStaleState] = mapped_column(
        enum_column(ClaimStaleState), nullable=False, default=ClaimStaleState.UNKNOWN
    )


class SourceAnchorRow(Base):
    __tablename__ = "source_anchors"
    __table_args__ = (
        Index("ix_source_anchors_repo_path", "repository_stable_key", "path"),
        Index("ix_source_anchors_freshness", "freshness_state", "checked_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    repository_stable_key: Mapped[str] = mapped_column(String(128), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(2000), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(80))
    symbol_fqn: Mapped[str | None] = mapped_column(String(1000))
    symbol_kind: Mapped[str | None] = mapped_column(String(120))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    evidence_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    freshness_state: Mapped[FreshnessState] = mapped_column(
        enum_column(FreshnessState), nullable=False, default=FreshnessState.UNKNOWN
    )
    cached_head: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClaimEvidenceRow(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (UniqueConstraint("claim_id", "source_id", "evidence_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    evidence_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_anchor_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_anchors.id", ondelete="SET NULL")
    )
    support_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class ClaimRelationRow(Base):
    __tablename__ = "claim_relations"
    __table_args__ = (
        UniqueConstraint("from_claim_id", "to_claim_id", "relation_type", name="uq_claim_relation"),
        Index("ix_claim_relations_from", "from_claim_id", "relation_type"),
        Index("ix_claim_relations_to", "to_claim_id", "relation_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    to_claim_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[ClaimRelationType] = mapped_column(
        enum_column(ClaimRelationType), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    method: Mapped[RelationMethod] = mapped_column(enum_column(RelationMethod), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RetrievalRunRow(Base):
    __tablename__ = "retrieval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    task: Mapped[str | None] = mapped_column(Text)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    selected_memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    candidate_features: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    context_manifest: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MemoryFeedbackRow(Base):
    __tablename__ = "memory_feedback"
    __table_args__ = (Index("ix_feedback_memory", "memory_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    retrieval_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("retrieval_runs.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    helpful: Mapped[FeedbackValue] = mapped_column(enum_column(FeedbackValue), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConsolidationCandidateRow(Base):
    __tablename__ = "consolidation_candidates"
    __table_args__ = (Index("ix_consolidation_scope_status", "scope_key", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[ScopeType] = mapped_column(enum_column(ScopeType), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    subject_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(String(120), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    source_memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    counterevidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
