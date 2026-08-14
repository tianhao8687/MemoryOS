"""Separate immutable source-anchor baselines from mutable observations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_anchor_observation_hardening"
down_revision = "0003_reality_intelligence_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_anchors", sa.Column("observed_head", sa.String(64)))
    op.add_column("source_anchors", sa.Column("observed_path", sa.String(2000)))
    op.add_column("source_anchors", sa.Column("observed_line_start", sa.Integer()))
    op.add_column("source_anchors", sa.Column("observed_line_end", sa.Integer()))
    op.add_column("source_anchors", sa.Column("observed_excerpt_hash", sa.String(64)))
    op.add_column("source_anchors", sa.Column("observed_at", sa.DateTime(timezone=True)))

    connection = op.get_bind()
    metadata = sa.MetaData()
    anchors = sa.Table("source_anchors", metadata, autoload_with=connection)
    connection.execute(
        anchors.update().values(
            observed_head=sa.func.coalesce(anchors.c.cached_head, anchors.c.commit_sha),
            observed_path=anchors.c.path,
            observed_line_start=anchors.c.line_start,
            observed_line_end=anchors.c.line_end,
            observed_excerpt_hash=anchors.c.excerpt_hash,
            observed_at=sa.func.coalesce(anchors.c.checked_at, anchors.c.created_at),
        )
    )
    op.create_index(
        "ix_entities_scope_name",
        "entities",
        ["scope_type", "scope_key", "normalized_name"],
    )
    op.create_index(
        "ix_claims_subject_status_recorded",
        "claims",
        ["subject_entity_id", "status", "stale_state", "recorded_at", "memory_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_claims_subject_status_recorded", table_name="claims")
    op.drop_index("ix_entities_scope_name", table_name="entities")
    with op.batch_alter_table("source_anchors") as batch_op:
        batch_op.drop_column("observed_at")
        batch_op.drop_column("observed_excerpt_hash")
        batch_op.drop_column("observed_line_end")
        batch_op.drop_column("observed_line_start")
        batch_op.drop_column("observed_path")
        batch_op.drop_column("observed_head")
