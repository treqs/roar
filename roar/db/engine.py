"""
SQLAlchemy engine and session configuration.

Handles database connection setup with SQLite-specific optimizations.
"""

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

# Try to import sqlite3, fall back to pysqlite3-binary if unavailable
# (uv's standalone Python builds may not have sqlite3 compiled in)
try:
    import sqlite3 as sqlite_module
except ImportError:
    import pysqlite3 as sqlite_module  # type: ignore[import-not-found, no-redef]


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode and foreign keys for SQLite connections."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


def create_roar_engine(db_path: Path) -> Engine:
    """
    Create SQLAlchemy engine for the given database path.

    Args:
        db_path: Path to SQLite database file

    Returns:
        Configured SQLAlchemy Engine
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        module=sqlite_module,
        echo=False,  # Set to True for SQL debugging
    )
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    Create a session factory for the engine.

    Args:
        engine: SQLAlchemy Engine

    Returns:
        Configured sessionmaker
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    """
    Initialize database schema.

    Uses create_all() for declarative schema creation.
    Also creates FTS5 virtual table and triggers for job search.

    Args:
        engine: SQLAlchemy Engine
    """
    Base.metadata.create_all(engine)

    # Create FTS5 virtual table for job search (requires raw SQL)
    with engine.connect() as conn:
        # Create FTS5 table
        conn.execute(
            text(
                """
            CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
                command,
                script,
                content=jobs,
                content_rowid=id
            )
        """
            )
        )

        # Create FTS triggers for INSERT
        conn.execute(
            text(
                """
            CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
                INSERT INTO jobs_fts(rowid, command, script)
                VALUES (new.id, new.command, new.script);
            END
        """
            )
        )

        # Create FTS triggers for DELETE
        conn.execute(
            text(
                """
            CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
                INSERT INTO jobs_fts(jobs_fts, rowid, command, script)
                VALUES ('delete', old.id, old.command, old.script);
            END
        """
            )
        )

        # Create FTS triggers for UPDATE
        conn.execute(
            text(
                """
            CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
                INSERT INTO jobs_fts(jobs_fts, rowid, command, script)
                VALUES ('delete', old.id, old.command, old.script);
                INSERT INTO jobs_fts(rowid, command, script)
                VALUES (new.id, new.command, new.script);
            END
        """
            )
        )

        conn.commit()
