from __future__ import annotations

import sqlite3
from pathlib import Path


class OptionalSqliteAnnIndex:
    """Persistent sqlite-vec KNN index with an explicit unavailable state."""

    name = "sqlite-vec"

    def __init__(self, path: Path | str, dimensions: int, *, enabled: bool = True) -> None:
        if not 1 <= dimensions <= 65_536:
            raise ValueError("vector dimensions must be between 1 and 65536")
        self.path = Path(path)
        self.dimensions = dimensions
        self.available = False
        self.unavailable_reason: str | None = "disabled"
        self._connection: sqlite3.Connection | None = None
        self._serialize: object | None = None
        if not enabled:
            return
        try:
            import sqlite_vec  # type: ignore[import-untyped]

            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ann_item_ids ("
                "rowid INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL UNIQUE)"
            )
            connection.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS ann_vectors "
                f"USING vec0(embedding float[{dimensions}])"
            )
            self._connection = connection
            self._serialize = sqlite_vec.serialize_float32
            self.available = True
            self.unavailable_reason = None
        except (ImportError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.unavailable_reason = str(exc)
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def upsert(self, vectors: dict[str, list[float]]) -> int:
        if not self.available or self._connection is None:
            return 0
        written = 0
        with self._connection:
            for item_id, vector in vectors.items():
                if len(vector) != self.dimensions:
                    continue
                self._connection.execute(
                    "INSERT INTO ann_item_ids(item_id) VALUES (?) ON CONFLICT(item_id) DO NOTHING",
                    (item_id,),
                )
                rowid = self._connection.execute(
                    "SELECT rowid FROM ann_item_ids WHERE item_id = ?", (item_id,)
                ).fetchone()
                if rowid is None:
                    continue
                self._connection.execute(
                    "INSERT OR REPLACE INTO ann_vectors(rowid, embedding) VALUES (?, ?)",
                    (int(rowid[0]), self._serialized(vector)),
                )
                written += 1
        return written

    def search(self, query: list[float], *, limit: int) -> list[tuple[str, float]]:
        if (
            not self.available
            or self._connection is None
            or len(query) != self.dimensions
            or limit <= 0
        ):
            return []
        try:
            rows = self._connection.execute(
                "SELECT ids.item_id, vectors.distance "
                "FROM ann_vectors AS vectors "
                "JOIN ann_item_ids AS ids ON ids.rowid = vectors.rowid "
                "WHERE vectors.embedding MATCH ? AND k = ? ORDER BY vectors.distance",
                (self._serialized(query), limit),
            ).fetchall()
        except sqlite3.Error as exc:
            self.unavailable_reason = str(exc)
            return []
        return [(str(item_id), 1.0 / (1.0 + float(distance))) for item_id, distance in rows]

    def count(self) -> int:
        if not self.available or self._connection is None:
            return 0
        try:
            return int(self._connection.execute("SELECT count(*) FROM ann_item_ids").fetchone()[0])
        except sqlite3.Error as exc:
            self.unavailable_reason = str(exc)
            return 0

    def clear(self) -> bool:
        if not self.available or self._connection is None:
            return False
        try:
            with self._connection:
                self._connection.execute("DELETE FROM ann_vectors")
                self._connection.execute("DELETE FROM ann_item_ids")
            return True
        except sqlite3.Error as exc:
            self.unavailable_reason = str(exc)
            return False

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _serialized(self, vector: list[float]) -> bytes:
        if not callable(self._serialize):
            raise RuntimeError("sqlite-vec serializer is unavailable")
        value = self._serialize(vector)
        if not isinstance(value, bytes):
            raise TypeError("sqlite-vec serializer returned an invalid value")
        return value


__all__ = ["OptionalSqliteAnnIndex"]
