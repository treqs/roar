"""
Storage backend abstraction.

Provides an abstract interface for database storage, enabling
future support for different backends (SQLite, PostgreSQL, etc.)
while maintaining a consistent API.

.. deprecated::
    This module is deprecated. Use :mod:`roar.db.context.DatabaseContext` instead.
    The SQLiteStorage class will be removed in a future version.
"""

import sqlite3
import warnings
from contextlib import contextmanager
from pathlib import Path

from ..core.exceptions import DatabaseConnectionError
from .schema import SCHEMA, run_migrations

# Emit deprecation warning when module is imported
warnings.warn(
    "roar.db.storage is deprecated. Use roar.db.context.DatabaseContext instead.",
    DeprecationWarning,
    stacklevel=2,
)


class SQLiteStorage:
    """
    SQLite storage backend.

    Encapsulates SQLite connection management and provides
    a consistent interface for database operations.
    """

    def __init__(self, db_path: Path):
        """
        Initialize storage with database path.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Connect to the database and initialize schema if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        run_migrations(self._conn)
        self._conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        """
        Get the active connection.

        Raises:
            DatabaseConnectionError: If not connected
        """
        if self._conn is None:
            raise DatabaseConnectionError(
                "Database not connected. Call connect() first.",
                db_path=str(self.db_path),
            )
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """
        Execute SQL and return cursor.

        Args:
            sql: SQL statement
            params: Query parameters

        Returns:
            Cursor with results
        """
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """
        Execute SQL with multiple parameter sets.

        Args:
            sql: SQL statement
            params_list: List of parameter tuples
        """
        self.conn.executemany(sql, params_list)

    def executescript(self, sql: str) -> None:
        """
        Execute multiple SQL statements.

        Args:
            sql: SQL script
        """
        self.conn.executescript(sql)

    def commit(self) -> None:
        """Commit current transaction."""
        self.conn.commit()

    def rollback(self) -> None:
        """Rollback current transaction."""
        self.conn.rollback()

    @contextmanager
    def transaction(self):
        """
        Context manager for transactions.

        Automatically commits on success, rolls back on exception.

        Example:
            with storage.transaction():
                storage.execute("INSERT INTO ...")
                storage.execute("UPDATE ...")
        """
        try:
            yield
            self.commit()
        except Exception:
            self.rollback()
            raise

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
