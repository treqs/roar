"""
Service protocol definitions for business logic layer.

These protocols define the contracts for services that orchestrate
business operations across repositories.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HashingService(Protocol):
    """Service for computing and caching file hashes."""

    def compute_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """Compute hash for file, using cache if valid."""
        ...

    def compute_file_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """Compute hash for file (alias for compute_hash)."""
        ...

    def compute_hashes(
        self, path: str, algorithms: list[str] | None = None
    ) -> dict[str, str] | None:
        """Compute multiple hashes in single pass."""
        ...


@runtime_checkable
class JobRecordingService(Protocol):
    """Service for recording jobs with their artifacts."""

    def record(
        self,
        command: str,
        timestamp: float,
        input_files: list[str] | None = None,
        output_files: list[str] | None = None,
        git_repo: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        metadata: str | None = None,
        step_name: str | None = None,
        assign_to_session: bool = True,
        job_type: str | None = None,
        repo_root: str | None = None,
        telemetry: str | None = None,
        hash_algorithms: list[str] | None = None,
    ) -> tuple:
        """Record job with all inputs/outputs. Returns (job_id, job_uid)."""
        ...


@runtime_checkable
class SessionService(Protocol):
    """Service for session management and analysis."""

    def get_summary(self, session_id: int) -> dict[str, Any]:
        """Get session summary with all steps."""
        ...

    def get_stale_steps(self, session_id: int) -> list[int]:
        """Find steps with stale upstream dependencies."""
        ...

    def get_stale_artifacts(self, session_id: int) -> list[str]:
        """Return artifact IDs that are stale (produced by stale steps)."""
        ...

    def get_downstream_steps(self, session_id: int, step_number: int) -> list[int]:
        """Find all steps that depend on given step's outputs."""
        ...

    def check_git_consistency(self, session_id: int) -> dict[str, Any]:
        """Check if session has mixed git commits."""
        ...

    def compute_step_identity(
        self,
        input_paths: list[str],
        output_paths: list[str],
        repo_root: str | None = None,
        command: str | None = None,
    ) -> str:
        """Compute step identity hash from paths."""
        ...


@runtime_checkable
class LineageService(Protocol):
    """Service for artifact lineage queries."""

    def get_artifact_lineage(self, artifact_id: str, depth: int = 3) -> dict[str, Any]:
        """Trace artifact's production lineage up to depth levels."""
        ...

    def get_lineage_jobs(
        self, artifact_ids: list[str], max_depth: int = 10
    ) -> list[dict[str, Any]]:
        """Get all jobs in lineage DAG for given artifacts."""
        ...
