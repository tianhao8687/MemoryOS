"""Add Memory Intelligence V2 claim graph and evaluation state."""

from alembic import op

from memoryos.db import models  # noqa: F401
from memoryos.db.base import Base

revision = "0002_memory_intelligence"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


V2_TABLES = [
    "memory_feedback",
    "consolidation_candidates",
    "retrieval_runs",
    "claim_relations",
    "claim_evidence",
    "source_anchors",
    "claims",
    "entity_merge_events",
    "entities",
]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    for table_name in V2_TABLES:
        table = Base.metadata.tables[table_name]
        table.drop(bind=connection, checkfirst=True)
