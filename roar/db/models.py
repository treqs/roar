"""
SQLAlchemy ORM models for roar database.

Defines all database tables as mapped classes with relationships.
"""

from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class Artifact(Base):
    """Content-addressed file artifact."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen_path: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(Text)
    uploaded_to: Mapped[str | None] = mapped_column(Text)  # JSON list
    synced_at: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)  # JSON

    # Relationships
    hashes: Mapped[list["ArtifactHash"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )
    job_inputs: Mapped[list["JobInput"]] = relationship(back_populates="artifact")
    job_outputs: Mapped[list["JobOutput"]] = relationship(back_populates="artifact")
    collection_members: Mapped[list["CollectionMember"]] = relationship(back_populates="artifact")

    __table_args__ = (
        Index("idx_artifacts_first_seen", "first_seen_at"),
        Index("idx_artifacts_synced", "synced_at"),
    )


class ArtifactHash(Base):
    """Hash digest for an artifact (multiple algorithms supported)."""

    __tablename__ = "artifact_hashes"

    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    digest: Mapped[str] = mapped_column(String, nullable=False)

    # Relationships
    artifact: Mapped["Artifact"] = relationship(back_populates="hashes")

    __table_args__ = (
        PrimaryKeyConstraint("algorithm", "digest"),
        Index("idx_artifact_hashes_artifact", "artifact_id"),
        Index("idx_artifact_hashes_digest", "digest"),
    )


class Session(Base):
    """Ordered sequence of steps for reproducibility."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash: Mapped[str | None] = mapped_column(String, unique=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    source_artifact_hash: Mapped[str | None] = mapped_column(String)
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[int] = mapped_column(Integer, default=0)
    git_repo: Mapped[str | None] = mapped_column(Text)
    git_commit_start: Mapped[str | None] = mapped_column(String)
    git_commit_end: Mapped[str | None] = mapped_column(String)
    synced_at: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)  # YAML content

    # Relationships
    jobs: Mapped[list["Job"]] = relationship(back_populates="session")

    __table_args__ = (
        Index("idx_sessions_hash", "hash"),
        Index("idx_sessions_source", "source_artifact_hash"),
        Index("idx_sessions_active", "is_active"),
    )


class Job(Base):
    """Execution record that consumes inputs and produces outputs."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_uid: Mapped[str | None] = mapped_column(String, unique=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    script: Mapped[str | None] = mapped_column(String)
    step_identity: Mapped[str | None] = mapped_column(String)
    session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sessions.id"))
    step_number: Mapped[int | None] = mapped_column(Integer)
    step_name: Mapped[str | None] = mapped_column(String)
    git_repo: Mapped[str | None] = mapped_column(Text)
    git_commit: Mapped[str | None] = mapped_column(String)
    git_branch: Mapped[str | None] = mapped_column(String)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String)
    job_type: Mapped[str | None] = mapped_column(String)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)  # JSON
    telemetry: Mapped[str | None] = mapped_column(Text)  # JSON

    # Relationships
    session: Mapped[Optional["Session"]] = relationship(back_populates="jobs")
    inputs: Mapped[list["JobInput"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    outputs: Mapped[list["JobOutput"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_jobs_timestamp", "timestamp"),
        Index("idx_jobs_script", "script"),
        Index("idx_jobs_git_commit", "git_commit"),
        Index("idx_jobs_synced", "synced_at"),
        Index("idx_jobs_session", "session_id"),
        Index("idx_jobs_step_identity", "step_identity"),
    )


class JobInput(Base):
    """Association between job and input artifact."""

    __tablename__ = "job_inputs"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(String, ForeignKey("artifacts.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="inputs")
    artifact: Mapped["Artifact"] = relationship(back_populates="job_inputs")

    __table_args__ = (
        PrimaryKeyConstraint("job_id", "artifact_id", "path"),
        Index("idx_job_inputs_artifact", "artifact_id"),
        Index("idx_job_inputs_path", "path"),
    )


class JobOutput(Base):
    """Association between job and output artifact."""

    __tablename__ = "job_outputs"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(String, ForeignKey("artifacts.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="outputs")
    artifact: Mapped["Artifact"] = relationship(back_populates="job_outputs")

    __table_args__ = (
        PrimaryKeyConstraint("job_id", "artifact_id", "path"),
        Index("idx_job_outputs_artifact", "artifact_id"),
        Index("idx_job_outputs_path", "path"),
    )


class Collection(Base):
    """Named set of artifacts and/or child collections."""

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    collection_type: Mapped[str | None] = mapped_column(String)
    source_type: Mapped[str | None] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(Text)
    uploaded_to: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    synced_at: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)  # JSON

    # Relationships
    members: Mapped[list["CollectionMember"]] = relationship(
        back_populates="collection",
        foreign_keys="CollectionMember.collection_id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_collections_name", "name"),
        Index("idx_collections_type", "collection_type"),
        Index("idx_collections_source", "source_url"),
    )


class CollectionMember(Base):
    """Membership in a collection (either artifact or child collection)."""

    __tablename__ = "collection_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str | None] = mapped_column(String, ForeignKey("artifacts.id"))
    child_collection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("collections.id", ondelete="CASCADE")
    )
    path_in_collection: Mapped[str | None] = mapped_column(Text)

    # Relationships
    collection: Mapped["Collection"] = relationship(
        back_populates="members", foreign_keys=[collection_id]
    )
    artifact: Mapped[Optional["Artifact"]] = relationship(back_populates="collection_members")
    child_collection: Mapped[Optional["Collection"]] = relationship(
        foreign_keys=[child_collection_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(artifact_id IS NULL) != (child_collection_id IS NULL)",
            name="chk_member_type",
        ),
        Index("idx_collection_members_collection", "collection_id"),
        Index("idx_collection_members_artifact", "artifact_id"),
        Index("idx_collection_members_child", "child_collection_id"),
    )


class HashCache(Base):
    """Local cache for file path to hash mapping."""

    __tablename__ = "hash_cache"

    path: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    digest: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime: Mapped[float] = mapped_column(Float, nullable=False)
    cached_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("path", "algorithm"),
        Index("idx_hash_cache_path", "path"),
        Index("idx_hash_cache_updated", "cached_at"),
    )


class SchemaVersion(Base):
    """Schema version tracking."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
