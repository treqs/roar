"""
VCS (Version Control System) domain models.

Provides Pydantic models for version control information.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, computed_field

from .base import RoarBaseModel


class VCSInfo(RoarBaseModel):
    """Version control system information.

    Contains comprehensive information about a repository's current state.
    """

    commit: Annotated[str, Field(min_length=7, max_length=40)] | None = None
    branch: str | None = None
    remote_url: str | None = None
    clean: bool = True
    uncommitted_changes: list[str] = Field(default_factory=list)
    commit_timestamp: str | None = None
    commit_message: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def short_commit(self) -> str | None:
        """Get short form of commit hash (7 characters)."""
        return self.commit[:7] if self.commit else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_changes(self) -> bool:
        """Check if there are uncommitted changes."""
        return not self.clean or len(self.uncommitted_changes) > 0
