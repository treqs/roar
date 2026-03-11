"""
SQLAlchemy repository implementations.

Provides concrete implementations of repository interfaces using SQLAlchemy
ORM, following the Repository pattern for clean data access.
"""

from .artifact import SQLAlchemyArtifactRepository, SQLiteArtifactRepository
from .collection import SQLAlchemyCollectionRepository, SQLiteCollectionRepository
from .composite import SQLAlchemyCompositeRepository
from .hash_cache import SQLAlchemyHashCacheRepository, SQLiteHashCacheRepository
from .job import SQLAlchemyJobRepository, SQLiteJobRepository
from .label import SQLAlchemyLabelRepository, SQLiteLabelRepository
from .session import SQLAlchemySessionRepository, SQLiteSessionRepository

__all__ = [
    "SQLAlchemyArtifactRepository",
    "SQLAlchemyCollectionRepository",
    "SQLAlchemyCompositeRepository",
    # SQLAlchemy implementations (primary)
    "SQLAlchemyHashCacheRepository",
    "SQLAlchemyJobRepository",
    "SQLAlchemyLabelRepository",
    "SQLAlchemySessionRepository",
    "SQLiteArtifactRepository",
    "SQLiteCollectionRepository",
    # Backward compatibility aliases
    "SQLiteHashCacheRepository",
    "SQLiteJobRepository",
    "SQLiteLabelRepository",
    "SQLiteSessionRepository",
]
