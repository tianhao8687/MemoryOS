from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    CreatedBy,
    MemoryStatus,
    MemoryType,
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
