"""
Roar database layer.

This package provides the database layer for roar lineage tracking,
following SOLID principles:

- Models: SQLAlchemy ORM models for all entities
- Repositories: Focused data access interfaces
- Services: Business logic orchestration
- Hashing: Strategy pattern for hash algorithms
- Engine: SQLAlchemy engine and session configuration

Usage:
    Use the DatabaseContext for database access:

        from roar.db.context import create_database_context

        with create_database_context(roar_dir) as ctx:
            artifacts = ctx.artifacts.get_all()
            jobs = ctx.jobs.get_recent()
"""

from .context import DatabaseContext, create_database_context
from .engine import create_roar_engine, create_session_factory, init_database
from .models import (
    Artifact,
    ArtifactHash,
    Base,
    Collection,
    CollectionMember,
    HashCache,
    Job,
    JobInput,
    JobOutput,
    SchemaVersion,
    Session,
)

__all__ = [
    "Artifact",
    "ArtifactHash",
    # Models
    "Base",
    "Collection",
    "CollectionMember",
    # Context
    "DatabaseContext",
    "HashCache",
    "Job",
    "JobInput",
    "JobOutput",
    "SchemaVersion",
    "Session",
    "create_database_context",
    # Engine
    "create_roar_engine",
    "create_session_factory",
    "init_database",
]
