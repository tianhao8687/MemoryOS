from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from memoryos.config import MemoryOSSettings


class Database:
    def __init__(self, settings: MemoryOSSettings) -> None:
        self.settings = settings
        settings.ensure_directories()
        database_url = f"sqlite+pysqlite:///{settings.database_path.as_posix()}"
        self.engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": settings.busy_timeout_ms / 1000},
            pool_pre_ping=True,
        )
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False, class_=Session
        )

    def _configure_sqlite(self, dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={self.settings.busy_timeout_ms}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def initialize(self) -> None:
        migrations = Path(__file__).resolve().parent / "migrations"
        config = Config()
        config.set_main_option("script_location", str(migrations))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def checkpoint(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    def integrity_check(self) -> str:
        with self.engine.connect() as connection:
            return str(connection.execute(text("PRAGMA integrity_check")).scalar_one())

    def schema_version(self) -> str:
        with self.engine.connect() as connection:
            result = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            return str(result)

    def close(self) -> None:
        self.engine.dispose()


def connection_from_config(config: Config) -> Connection | None:
    value = config.attributes.get("connection")
    return value if isinstance(value, Connection) else None


def engine_from_database(database: Database) -> Engine:
    return database.engine
