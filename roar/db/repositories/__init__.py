"""
SQLAlchemy repository implementations.

Provides concrete implementations of repository interfaces using SQLAlchemy
ORM, following the Repository pattern for clean data access.
"""

from .artifact import SQLAlchemyArtifactRepository, SQLiteArtifactRepository
from .collection import SQLAlchemyCollectionRepository, SQLiteCollectionRepository
from .hash_cache import SQLAlchemyHashCacheRepository, SQLiteHashCacheRepository
from .job import SQLAlchemyJobRepository, SQLiteJobRepository
from .session import SQLAlchemySessionRepository, SQLiteSessionRepository

__all__ = [
    "SQLAlchemyArtifactRepository",
    "SQLAlchemyCollectionRepository",
    # SQLAlchemy implementations (primary)
    "SQLAlchemyHashCacheRepository",
    "SQLAlchemyJobRepository",
    "SQLAlchemySessionRepository",
    "SQLiteArtifactRepository",
    "SQLiteCollectionRepository",
    # Backward compatibility aliases
    "SQLiteHashCacheRepository",
    "SQLiteJobRepository",
    "SQLiteSessionRepository",
]
