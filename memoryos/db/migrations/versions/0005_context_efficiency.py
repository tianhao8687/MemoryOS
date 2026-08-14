"""Add minimum-sufficient-context diagnostics and disposable snapshot cache."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_context_efficiency"
down_revision = "0004_anchor_observation_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("retrieval_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_usage_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "context_policy_manifest",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "context_diagnostics_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "context_shadow_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("base_snapshot_id", sa.String(36)),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("scope_fingerprint", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("tokenizer_id", sa.String(300), nullable=False),
        sa.Column("counter_kind", sa.String(32), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("full_text_sha256", sa.String(64), nullable=False),
        sa.Column("full_estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_context_snapshots_scope_expiry",
        "context_snapshots",
        ["scope_fingerprint", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_context_snapshots_scope_expiry", table_name="context_snapshots")
    op.drop_table("context_snapshots")
    with op.batch_alter_table("retrieval_runs") as batch_op:
        batch_op.drop_column("context_shadow_json")
        batch_op.drop_column("context_diagnostics_json")
        batch_op.drop_column("context_policy_manifest")
        batch_op.drop_column("context_usage_json")
