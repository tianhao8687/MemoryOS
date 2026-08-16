from __future__ import annotations

from alembic import context


def run_migrations() -> None:
    """Run migrations with the URL supplied by the deployment environment."""

    context.configure(url=context.config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()
