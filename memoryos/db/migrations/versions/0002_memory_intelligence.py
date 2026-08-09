"""Add the fixed Memory Intelligence V2 claim graph schema."""

import sqlalchemy as sa
from alembic import op

revision = "0002_memory_intelligence"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(1000), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False),
        sa.Column("aliases_json", sa.JSON(), nullable=False),
        sa.Column("stable_external_key", sa.String(1000)),
        sa.Column("redirect_to_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["redirect_to_id"], ["entities.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_key",
            "entity_type",
            "normalized_name",
            name="uq_entities_scoped_name",
        ),
    )
    op.create_index(
        "ix_entities_scope_type",
        "entities",
        ["scope_type", "scope_key", "entity_type"],
    )
    op.create_index(
        "ix_entities_stable_external_key",
        "entities",
        ["stable_external_key"],
    )
    op.create_table(
        "entity_merge_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_entity_id", sa.String(36), nullable=False),
        sa.Column("to_entity_id", sa.String(36), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("rationale", sa.Text()),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_entity_id"], ["entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_entity_id"], ["entities.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("subject_entity_id", sa.String(36), nullable=False),
        sa.Column("predicate", sa.String(120), nullable=False),
        sa.Column("object_kind", sa.String(32), nullable=False),
        sa.Column("object_entity_id", sa.String(36)),
        sa.Column("object_value", sa.JSON()),
        sa.Column("polarity", sa.String(32), nullable=False),
        sa.Column("modality", sa.String(32), nullable=False),
        sa.Column("qualifiers_json", sa.JSON(), nullable=False),
        sa.Column("canonical_key", sa.String(1200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale_state", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["object_entity_id"], ["entities.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_claims_canonical_key", "claims", ["canonical_key"])
    op.create_index(
        "ix_claims_subject_predicate_status",
        "claims",
        ["subject_entity_id", "predicate", "status"],
    )
    op.create_index("ix_claims_memory_status", "claims", ["memory_id", "status"])
    op.create_index(
        "ix_claims_temporal",
        "claims",
        ["valid_from", "valid_to", "recorded_at"],
    )
    op.create_table(
        "source_anchors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_stable_key", sa.String(128), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("path", sa.String(2000), nullable=False),
        sa.Column("blob_sha", sa.String(64), nullable=False),
        sa.Column("language", sa.String(80)),
        sa.Column("symbol_fqn", sa.String(1000)),
        sa.Column("symbol_kind", sa.String(120)),
        sa.Column("line_start", sa.Integer()),
        sa.Column("line_end", sa.Integer()),
        sa.Column("evidence_excerpt", sa.Text(), nullable=False),
        sa.Column("excerpt_hash", sa.String(64), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("freshness_state", sa.String(32), nullable=False),
        sa.Column("cached_head", sa.String(64)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_source_anchors_repo_path",
        "source_anchors",
        ["repository_stable_key", "path"],
    )
    op.create_index(
        "ix_source_anchors_freshness",
        "source_anchors",
        ["freshness_state", "checked_at"],
    )
    op.create_table(
        "claim_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("evidence_excerpt", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("source_anchor_id", sa.String(36)),
        sa.Column("support_weight", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_anchor_id"], ["source_anchors.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "claim_id",
            "source_id",
            "evidence_hash",
            name="uq_claim_evidence_source_hash",
        ),
    )
    op.create_index("ix_claim_evidence_evidence_hash", "claim_evidence", ["evidence_hash"])
    op.create_table(
        "claim_relations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("from_claim_id", sa.String(36), nullable=False),
        sa.Column("to_claim_id", sa.String(36), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["from_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "from_claim_id",
            "to_claim_id",
            "relation_type",
            name="uq_claim_relation",
        ),
    )
    op.create_index(
        "ix_claim_relations_from", "claim_relations", ["from_claim_id", "relation_type"]
    )
    op.create_index("ix_claim_relations_to", "claim_relations", ["to_claim_id", "relation_type"])
    op.create_table(
        "retrieval_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("task", sa.Text()),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("selected_memory_ids", sa.JSON(), nullable=False),
        sa.Column("candidate_features", sa.JSON(), nullable=False),
        sa.Column("context_manifest", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "consolidation_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(1000), nullable=False),
        sa.Column("subject_entity_id", sa.String(36), nullable=False),
        sa.Column("predicate", sa.String(120), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_memory_ids", sa.JSON(), nullable=False),
        sa.Column("counterevidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_consolidation_scope_status",
        "consolidation_candidates",
        ["scope_key", "status"],
    )
    op.create_table(
        "memory_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("retrieval_run_id", sa.String(36), nullable=False),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("helpful", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["retrieval_run_id"], ["retrieval_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_feedback_memory", "memory_feedback", ["memory_id", "created_at"])


def downgrade() -> None:
    for table_name in (
        "memory_feedback",
        "consolidation_candidates",
        "retrieval_runs",
        "claim_relations",
        "claim_evidence",
        "source_anchors",
        "claims",
        "entity_merge_events",
        "entities",
    ):
        op.drop_table(table_name)
