from __future__ import annotations

import sqlite3


def open_test_database():
    """Return the isolated unit-test fixture database."""

    return sqlite3.connect(":memory:")
