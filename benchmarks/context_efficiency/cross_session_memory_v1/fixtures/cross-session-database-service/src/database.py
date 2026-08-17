from __future__ import annotations

import os

from sqlalchemy import create_engine


def build_engine():
    database_url = os.environ["DATABASE_URL"]
    return create_engine(database_url, pool_pre_ping=True)
