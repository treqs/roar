"""
Database context for roar.

Provides a lightweight context manager that exposes typed repository
and service properties, replacing the monolithic RoarDB facade.
"""

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..core.exceptions import DatabaseConnectionError
from .engine import create_roar_engine, create_session_factory, init_database
from .hashing import HashAlgorithmRegistry
from .repositories import (
    SQLAlchemyArtifactRepository,
    SQLAlchemyCollectionRepository,
    SQLAlchemyHashCacheRepository,
    SQLAlchemyJobRepository,
    SQLAlchemySessionRepository,
)
from .services import (
    DefaultHashingService,
    DefaultLineageService,
    DefaultSessionService,
    JobRecordingService,
)


class DatabaseContext:
    """
    Context manager providing access to database repositories and services.

    Usage:
        with DatabaseContext(db_path) as ctx:
            session = ctx.sessions.get_active()
            jobs = ctx.jobs.get_recent(10)

        # Or via factory:
        with create_database_context(roar_dir) as ctx:
            ...
    """

    def __init__(self, db_path: Path):
        """
        Initialize DatabaseContext.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._engine: Engine | None = None
        self._session: Session | None = None
        self._hash_registry = HashAlgorithmRegistry()

        # Repositories (initialized on connect)
        self._hash_cache_repo: SQLAlchemyHashCacheRepository | None = None
        self._artifact_repo: SQLAlchemyArtifactRepository | None = None
        self._job_repo: SQLAlchemyJobRepository | None = None
        self._session_repo: SQLAlchemySessionRepository | None = None
        self._collection_repo: SQLAlchemyCollectionRepository | None = None

        # Services (initialized on connect)
        self._hashing_service: DefaultHashingService | None = None
        self._session_service: DefaultSessionService | None = None
        self._lineage_service: DefaultLineageService | None = None
        self._job_recording_service: JobRecordingService | None = None

    def connect(self) -> None:
        """Connect to the database and initialize schema if needed."""
        self._engine = create_roar_engine(self.db_path)
        init_database(self._engine)
        session_factory = create_session_factory(self._engine)
        self._session = session_factory()

        # Initialize repositories
        self._hash_cache_repo = SQLAlchemyHashCacheRepository(self._session)
        self._artifact_repo = SQLAlchemyArtifactRepository(self._session)
        self._job_repo = SQLAlchemyJobRepository(self._session)
        self._session_repo = SQLAlchemySessionRepository(self._session)
        self._collection_repo = SQLAlchemyCollectionRepository(self._session)

        # Initialize services
        self._hashing_service = DefaultHashingService(self._hash_cache_repo, self._hash_registry)
        self._session_service = DefaultSessionService(
            self._session_repo, self._job_repo, self._artifact_repo
        )
        self._lineage_service = DefaultLineageService(self._artifact_repo, self._job_repo)
        self._job_recording_service = JobRecordingService(
            self._session,
            self._job_repo,
            self._artifact_repo,
            self._session_repo,
            self._hashing_service,
            self._session_service,
        )

    def close(self) -> None:
        """Close database connection."""
        if self._session:
            self._session.close()
            self._session = None
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def commit(self) -> None:
        """Commit the current transaction."""
        if self._session:
            self._session.commit()

    def __enter__(self) -> "DatabaseContext":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session:
            if exc_type:
                self._session.rollback()
            else:
                self._session.commit()
        self.close()

    # -------------------------------------------------------------------------
    # Repository properties
    # -------------------------------------------------------------------------

    @property
    def session(self) -> Session:
        """Get the underlying database session."""
        if self._session is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._session

    @property
    def conn(self):
        """Get raw database connection for direct SQL queries.

        Note: Prefer using repositories when possible. This is for
        legacy compatibility with raw SQL usage.
        """
        if self._session is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._session.connection()

    @property
    def hash_cache(self) -> SQLAlchemyHashCacheRepository:
        """Hash cache repository for file hash caching."""
        if self._hash_cache_repo is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._hash_cache_repo

    @property
    def artifacts(self) -> SQLAlchemyArtifactRepository:
        """Artifact repository for content-addressed file storage."""
        if self._artifact_repo is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._artifact_repo

    @property
    def jobs(self) -> SQLAlchemyJobRepository:
        """Job repository for execution records."""
        if self._job_repo is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._job_repo

    @property
    def sessions(self) -> SQLAlchemySessionRepository:
        """Session repository for step sequences."""
        if self._session_repo is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._session_repo

    @property
    def collections(self) -> SQLAlchemyCollectionRepository:
        """Collection repository for artifact groups."""
        if self._collection_repo is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._collection_repo

    # -------------------------------------------------------------------------
    # Service properties
    # -------------------------------------------------------------------------

    @property
    def hashing(self) -> DefaultHashingService:
        """Hashing service for computing and caching file hashes."""
        if self._hashing_service is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._hashing_service

    @property
    def session_service(self) -> DefaultSessionService:
        """Session service for session management operations."""
        if self._session_service is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._session_service

    @property
    def lineage(self) -> DefaultLineageService:
        """Lineage service for artifact lineage queries."""
        if self._lineage_service is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._lineage_service

    @property
    def hash_registry(self) -> HashAlgorithmRegistry:
        """Hash algorithm registry for creating hashers."""
        return self._hash_registry

    @property
    def job_recording(self) -> JobRecordingService:
        """Job recording service for recording jobs with lineage."""
        if self._job_recording_service is None:
            raise DatabaseConnectionError(
                "DatabaseContext not connected. Use as context manager.",
                db_path=str(self.db_path),
            )
        return self._job_recording_service


def create_database_context(roar_dir: Path) -> DatabaseContext:
    """
    Create a DatabaseContext for the given .roar directory.

    This is the primary entry point for database access.

    Args:
        roar_dir: Path to the .roar directory

    Returns:
        DatabaseContext instance (use as context manager)
    """
    return DatabaseContext(roar_dir / "roar.db")
