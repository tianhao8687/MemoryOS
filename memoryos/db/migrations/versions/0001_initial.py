"""Initial MemoryOS V1 schema and FTS5 index."""

from alembic import op

from memoryos.db import models  # noqa: F401
from memoryos.db.base import Base
from memoryos.db.fts import create_fts, drop_fts

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    Base.metadata.create_all(bind=connection)
    create_fts(connection)


def downgrade() -> None:
    connection = op.get_bind()
    drop_fts(connection)
    Base.metadata.drop_all(bind=connection)
