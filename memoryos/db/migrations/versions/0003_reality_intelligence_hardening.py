"""Add V2.1 immutable truth history, review queue, ANN state, and memory health."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0003_reality_intelligence_hardening"
down_revision = "0002_memory_intelligence"
branch_labels = None
depends_on = None


def _stable_identity(
    scope_type: str,
    scope_key: str,
    subject_entity_id: str,
    predicate: str,
) -> str:
    value = "|".join((scope_type, scope_key, subject_entity_id, predicate))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def upgrade() -> None:
    identities = op.create_table(
        "claim_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(1000), nullable=False),
        sa.Column("subject_entity_id", sa.String(36), nullable=False),
        sa.Column("canonical_subject", sa.String(500), nullable=False),
        sa.Column("canonical_predicate", sa.String(120), nullable=False),
        sa.Column("stable_identity", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_key",
            "stable_identity",
            name="uq_claim_identity_scope_stable",
        ),
    )
    op.create_index(
        "ix_claim_identity_subject_predicate",
        "claim_identities",
        ["subject_entity_id", "canonical_predicate"],
    )
    versions = op.create_table(
        "claim_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("identity_id", sa.String(36), nullable=False),
        sa.Column("memory_id", sa.String(36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("object_kind", sa.String(32), nullable=False),
        sa.Column("object_entity_id", sa.String(36)),
        sa.Column("object_value", sa.JSON()),
        sa.Column("polarity", sa.String(32), nullable=False),
        sa.Column("modality", sa.String(32), nullable=False),
        sa.Column("qualifiers_json", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("transaction_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("transaction_to", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stale_state", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("source_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["identity_id"], ["claim_identities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["object_entity_id"], ["entities.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("claim_id", "version_number", name="uq_claim_version_number"),
    )
    op.create_index(
        "ix_claim_versions_identity_transaction",
        "claim_versions",
        ["identity_id", "transaction_from"],
    )
    op.create_index(
        "ix_claim_versions_claim_current",
        "claim_versions",
        ["claim_id", "transaction_to"],
    )
    op.create_index(
        "ix_claim_versions_bitemporal",
        "claim_versions",
        ["valid_from", "valid_to", "transaction_from"],
    )
    op.create_table(
        "possible_conflicts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("left_claim_id", sa.String(36), nullable=False),
        sa.Column("right_claim_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("deterministic_relationship", sa.String(64), nullable=False),
        sa.Column("deterministic_confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("model_result_json", sa.JSON(), nullable=False),
        sa.Column("provider_fingerprint", sa.String(500)),
        sa.Column("prompt_version", sa.String(120)),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(200)),
        sa.ForeignKeyConstraint(["left_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("left_claim_id", "right_claim_id", name="uq_possible_conflict_pair"),
    )
    op.create_index("ix_possible_conflicts_status", "possible_conflicts", ["status", "created_at"])
    op.create_table(
        "ann_index_state",
        sa.Column("namespace", sa.String(300), primary_key=True),
        sa.Column("backend", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("model", sa.String(300), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("unavailable_reason", sa.Text()),
        sa.Column("last_rebuild_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memory_health",
        sa.Column("memory_id", sa.String(36), primary_key=True),
        sa.Column("temperature", sa.String(32), nullable=False),
        sa.Column("health_score", sa.Float(), nullable=False),
        sa.Column("components_json", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("retrieval_count", sa.Integer(), nullable=False),
        sa.Column("last_retrieved_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_memory_health_temperature_score",
        "memory_health",
        ["temperature", "health_score"],
    )
    _backfill_claim_versions(identities, versions)


def _backfill_claim_versions(identities: sa.Table, versions: sa.Table) -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    claims = sa.Table("claims", metadata, autoload_with=connection)
    entities = sa.Table("entities", metadata, autoload_with=connection)
    rows = connection.execute(
        sa.select(
            claims,
            entities.c.scope_type.label("entity_scope_type"),
            entities.c.scope_key.label("entity_scope_key"),
            entities.c.normalized_name.label("canonical_subject"),
        ).join(entities, entities.c.id == claims.c.subject_entity_id)
    ).mappings()
    identity_ids: dict[tuple[str, str, str], str] = {}
    for row in rows:
        stable = _stable_identity(
            str(row["entity_scope_type"]),
            str(row["entity_scope_key"]),
            str(row["subject_entity_id"]),
            str(row["predicate"]),
        )
        key = (str(row["entity_scope_type"]), str(row["entity_scope_key"]), stable)
        identity_id = identity_ids.get(key)
        if identity_id is None:
            identity_id = str(uuid.uuid4())
            identity_ids[key] = identity_id
            connection.execute(
                identities.insert().values(
                    id=identity_id,
                    scope_type=row["entity_scope_type"],
                    scope_key=row["entity_scope_key"],
                    subject_entity_id=row["subject_entity_id"],
                    canonical_subject=row["canonical_subject"],
                    canonical_predicate=row["predicate"],
                    stable_identity=stable,
                    created_at=row["recorded_at"],
                )
            )
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "claim_id": row["id"],
            "identity_id": identity_id,
            "memory_id": row["memory_id"],
            "version_number": 1,
            "object_kind": row["object_kind"],
            "object_entity_id": row["object_entity_id"],
            "object_value": row["object_value"],
            "polarity": row["polarity"],
            "modality": row["modality"],
            "qualifiers_json": row["qualifiers_json"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "transaction_from": row["recorded_at"],
            "transaction_to": None,
            "status": row["status"],
            "stale_state": row["stale_state"],
            "confidence": row["confidence"],
            "reason": "V2.1 migration backfill",
            "actor": "migration:0003",
            "source_event_id": None,
            "created_at": row["recorded_at"],
        }
        connection.execute(versions.insert().values(**payload))


def downgrade() -> None:
    for table_name in (
        "memory_health",
        "ann_index_state",
        "possible_conflicts",
        "claim_versions",
        "claim_identities",
    ):
        op.drop_table(table_name)
