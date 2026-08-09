from __future__ import annotations

from sqlalchemy import Connection, text

FTS_STATEMENTS = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        memory_id UNINDEXED,
        title,
        content,
        subject,
        memory_key,
        category,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
        INSERT INTO memory_fts(memory_id, title, content, subject, memory_key, category)
        VALUES (new.id, new.title, new.content, coalesce(new.subject, ''),
                coalesce(new.key, ''), new.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
        DELETE FROM memory_fts WHERE memory_id = old.id;
        INSERT INTO memory_fts(memory_id, title, content, subject, memory_key, category)
        VALUES (new.id, new.title, new.content, coalesce(new.subject, ''),
                coalesce(new.key, ''), new.category);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
        DELETE FROM memory_fts WHERE memory_id = old.id;
    END
    """,
]


def create_fts(connection: Connection) -> None:
    for statement in FTS_STATEMENTS:
        connection.execute(text(statement))
    connection.execute(
        text(
            """
            INSERT INTO memory_fts(memory_id, title, content, subject, memory_key, category)
            SELECT m.id, m.title, m.content, coalesce(m.subject, ''),
                   coalesce(m.key, ''), m.category
            FROM memories m
            WHERE NOT EXISTS (SELECT 1 FROM memory_fts f WHERE f.memory_id = m.id)
            """
        )
    )


def drop_fts(connection: Connection) -> None:
    for name in ("memories_fts_insert", "memories_fts_update", "memories_fts_delete"):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    connection.execute(text("DROP TABLE IF EXISTS memory_fts"))
