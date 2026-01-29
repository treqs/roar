"""
Repository protocol definitions for data access layer.

These protocols define focused interfaces for data access operations,
following the Interface Segregation Principle (ISP).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArtifactRepository(Protocol):
    """Repository for artifact storage and retrieval."""

    def register(
        self,
        hashes: dict[str, str],
        size: int,
        path: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> tuple:
        """Register artifact, returns (artifact_id, created)."""
        ...

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        """Get artifact by ID."""
        ...

    def get_by_hash(self, digest: str, algorithm: str | None = None) -> dict[str, Any] | None:
        """Get artifact by hash digest (full or prefix)."""
        ...

    def get_hashes(self, artifact_id: str) -> list[dict[str, Any]]:
        """Get all hashes for an artifact."""
        ...

    def get_locations(self, artifact_id: str) -> list[dict[str, str]]:
        """Get all known locations for an artifact."""
        ...

    def get_recent_outputs(
        self, limit: int = 50, job_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recently output artifacts."""
        ...

    def update_upload(self, artifact_id: str, uploaded_to: str) -> None:
        """Update artifact with upload location."""
        ...

    def get_jobs(self, artifact_id: str) -> dict[str, list[dict[str, Any]]]:
        """Get jobs that produced or consumed an artifact."""
        ...

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        """Get artifact by file path."""
        ...


@runtime_checkable
class JobRepository(Protocol):
    """Repository for job storage and retrieval."""

    def create(
        self,
        command: str,
        timestamp: float,
        step_identity: str | None = None,
        session_id: int | None = None,
        step_number: int | None = None,
        step_name: str | None = None,
        git_repo: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        metadata: str | None = None,
        job_type: str | None = None,
        telemetry: str | None = None,
    ) -> tuple:
        """Create job record, returns (job_id, job_uid)."""
        ...

    def get(self, job_id: int) -> dict[str, Any] | None:
        """Get job by ID."""
        ...

    def get_by_uid(self, job_uid: str) -> dict[str, Any] | None:
        """Get job by UID."""
        ...

    def get_inputs(self, job_id: int, artifact_repo: "ArtifactRepository") -> list[dict[str, Any]]:
        """Get job input artifacts."""
        ...

    def get_outputs(self, job_id: int, artifact_repo: "ArtifactRepository") -> list[dict[str, Any]]:
        """Get job output artifacts."""
        ...

    def add_input(self, job_id: int, artifact_id: str, path: str) -> None:
        """Add input artifact to job."""
        ...

    def add_output(self, job_id: int, artifact_id: str, path: str) -> None:
        """Add output artifact to job."""
        ...

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent jobs."""
        ...

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search jobs by command/script."""
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Repository for session/DAG storage and retrieval."""

    def create(
        self,
        source_artifact_hash: str | None = None,
        git_repo: str | None = None,
        git_commit: str | None = None,
        make_active: bool = True,
    ) -> int:
        """Create session, returns session_id."""
        ...

    def get(self, session_id: int) -> dict[str, Any] | None:
        """Get session by ID."""
        ...

    def get_active(self) -> dict[str, Any] | None:
        """Get the currently active session."""
        ...

    def get_or_create_active(self) -> int:
        """Get active session or create one if none exists."""
        ...

    def set_active(self, session_id: int) -> None:
        """Set a session as active."""
        ...

    def get_steps(self, session_id: int) -> list[dict[str, Any]]:
        """Get all steps in a session."""
        ...

    def get_step_by_identity(self, session_id: int, step_identity: str) -> dict[str, Any] | None:
        """Get step by identity hash."""
        ...

    def get_step_by_number(
        self, session_id: int, step_number: int, job_type: str | None = None
    ) -> dict[str, Any] | None:
        """Get step by number."""
        ...

    def get_next_step_number(self, session_id: int) -> int:
        """Get next available step number."""
        ...

    def update_hash(self, session_id: int, session_hash: str) -> None:
        """Update session hash."""
        ...

    def update_current_step(self, session_id: int, step_number: int) -> None:
        """Update current step number."""
        ...

    def compute_step_identity(
        self,
        input_paths: list[str],
        output_paths: list[str],
        repo_root: str | None = None,
        command: str | None = None,
    ) -> str:
        """Compute a deterministic identity for a step."""
        ...

    def get_summary(self, session_id: int, job_repo: "JobRepository") -> dict[str, Any] | None:
        """Get a summary of a session for display."""
        ...

    def check_git_consistency(self, session_id: int) -> dict[str, Any]:
        """Check if a session has mixed git commits."""
        ...


@runtime_checkable
class HashCacheRepository(Protocol):
    """Repository for hash cache storage and retrieval."""

    def get_cached_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """Get cached hash if valid (mtime/size match)."""
        ...

    def get_cached_hashes(self, path: str) -> dict[str, str]:
        """Get all cached hashes for path if valid."""
        ...

    def cache_hash(self, path: str, algorithm: str, digest: str, size: int, mtime: float) -> None:
        """Store a hash in the cache."""
        ...

    def cache_hashes(self, path: str, hashes: dict[str, str], size: int, mtime: float) -> None:
        """Store multiple hashes for a file."""
        ...

    def invalidate(self, path: str, algorithm: str | None = None) -> None:
        """Invalidate cached hash(es)."""
        ...

    def clean_stale(self, max_age_days: int = 30) -> None:
        """Remove stale cache entries."""
        ...


@runtime_checkable
class CollectionRepository(Protocol):
    """Repository for collection storage and retrieval."""

    def create(
        self,
        name: str,
        collection_type: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """Create collection, returns collection_id."""
        ...

    def get(self, collection_id: int) -> dict[str, Any] | None:
        """Get collection by ID."""
        ...

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Get collection by name."""
        ...

    def add_artifact(
        self,
        collection_id: int,
        artifact_id: str,
        path_in_collection: str | None = None,
    ) -> None:
        """Add artifact to collection."""
        ...

    def add_child(
        self,
        parent_id: int,
        child_id: int,
        path_in_collection: str | None = None,
    ) -> None:
        """Add child collection."""
        ...

    def get_members(
        self, collection_id: int, artifact_repo: "ArtifactRepository"
    ) -> dict[str, list[dict[str, Any]]]:
        """Get collection members (artifacts and children)."""
        ...
