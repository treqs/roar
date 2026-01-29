"""
Session domain models.

Provides Pydantic models for DAG sessions (ordered sequences of steps).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from .base import RoarBaseModel

if TYPE_CHECKING:
    from .job import Job


class Session(RoarBaseModel):
    """Represents a DAG session (ordered sequence of steps).

    Sessions group related jobs together and track the execution
    sequence for reproducibility.
    """

    id: int
    hash: Annotated[str, Field(min_length=8, max_length=64)] | None = None
    created_at: Annotated[float, Field(gt=0)]
    source_artifact_hash: str | None = None
    current_step: Annotated[int, Field(ge=1)] = 1
    is_active: bool = False
    git_repo: str | None = None
    git_commit_start: Annotated[str, Field(min_length=7, max_length=40)] | None = None
    git_commit_end: Annotated[str, Field(min_length=7, max_length=40)] | None = None
    metadata: str | None = None
    step_count: Annotated[int, Field(ge=0)] = 0
    jobs: list[Job] = Field(default_factory=list)

    @classmethod
    def from_orm(
        cls,
        orm_session: object,
        jobs: list[Job] | None = None,
    ) -> Session:
        """Create Session from ORM model.

        Args:
            orm_session: SQLAlchemy Session model instance
            jobs: List of Job pydantic models

        Returns:
            Session pydantic model instance
        """
        return cls(
            id=orm_session.id,  # type: ignore[attr-defined]
            hash=orm_session.hash,  # type: ignore[attr-defined]
            created_at=orm_session.created_at,  # type: ignore[attr-defined]
            source_artifact_hash=orm_session.source_artifact_hash,  # type: ignore[attr-defined]
            current_step=orm_session.current_step,  # type: ignore[attr-defined]
            is_active=bool(orm_session.is_active),  # type: ignore[attr-defined]
            git_repo=orm_session.git_repo,  # type: ignore[attr-defined]
            git_commit_start=orm_session.git_commit_start,  # type: ignore[attr-defined]
            git_commit_end=orm_session.git_commit_end,  # type: ignore[attr-defined]
            metadata=getattr(orm_session, "metadata_", None),
            jobs=jobs or [],
        )
