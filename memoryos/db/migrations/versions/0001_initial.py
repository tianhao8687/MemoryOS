"""Initial MemoryOS V1 schema and FTS5 index.

This migration is intentionally self-contained.  Importing the live ORM metadata
would make a fresh V1 database silently acquire tables added by later releases.
"""

import sqlalchemy as sa
from alembic import op

from memoryos.db.fts import create_fts, drop_fts

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("path", sa.String(2000), nullable=False),
        sa.Column("remote_url", sa.String(2000)),
        sa.Column("default_branch", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("stable_key", name="uq_repositories_stable_key"),
    )
    op.create_index("ix_repositories_stable_key", "repositories", ["stable_key"], unique=True)
    op.create_table(
        "memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(1000), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("category", sa.String(120), nullable=False),
        sa.Column("subject", sa.String(300)),
        sa.Column("key", sa.String(300)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("ttl_seconds", sa.Integer()),
        sa.Column("supersedes_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("sensitivity", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["supersedes_id"], ["memories.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_memories_scope_status",
        "memories",
        ["scope_type", "scope_key", "status"],
    )
    op.create_index(
        "ix_memories_semantic_key",
        "memories",
        ["scope_type", "scope_key", "key", "status"],
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(1000), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sources_content_hash", "sources", ["content_hash"])
    op.create_table(
        "memory_sources",
        sa.Column("memory_id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), primary_key=True),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "relations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_memory_id", sa.String(36), nullable=False),
        sa.Column("to_memory_id", sa.String(36), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "from_memory_id",
            "to_memory_id",
            "relation_type",
            name="uq_relations_pair_type",
        ),
    )
    op.create_table(
        "embeddings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("model", sa.String(300), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector_blob", sa.LargeBinary()),
        sa.Column("vector_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("memory_id", name="uq_embeddings_memory_id"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_entity_id", "audit_events", ["entity_id"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_fts(op.get_bind())


def downgrade() -> None:
    drop_fts(op.get_bind())
    for table_name in (
        "settings",
        "audit_events",
        "embeddings",
        "relations",
        "memory_sources",
        "sources",
        "memories",
        "repositories",
    ):
        op.drop_table(table_name)
