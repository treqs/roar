"""Shared typed models for local/remote lookup workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar


class LookupSource(str, Enum):
    """Where a lookup result came from."""

    LOCAL = "local"
    REMOTE = "remote"
    NONE = "none"


class RefKind(str, Enum):
    """Normalized reference kinds shared by query commands."""

    JOB_STEP = "job_step"
    FILE_PATH = "file_path"
    JOB_UID = "job_uid"
    ARTIFACT_HASH = "artifact_hash"
    PATH_CANDIDATE = "path_candidate"
    SESSION = "session"


@dataclass(frozen=True)
class ParsedRef:
    """Parsed CLI reference with normalized kind and selector."""

    raw: str
    kind: RefKind
    selector: str = "auto"

    @property
    def is_artifact_lookup_candidate(self) -> bool:
        """Return whether this ref can participate in artifact remote fallback."""
        return self.selector == "artifact" or self.kind == RefKind.ARTIFACT_HASH


T = TypeVar("T")


@dataclass(frozen=True)
class LookupResult(Generic[T]):
    """Generic local/remote lookup result."""

    value: T | None = None
    error: str | None = None
    source: LookupSource = LookupSource.NONE
