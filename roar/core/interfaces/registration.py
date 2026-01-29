"""
Protocol interfaces for registration services.

These protocols define contracts for session, artifact, and job registration
with GLaaS, following the 4-phase registration pattern:
1. Session - Register session with git context
2. Jobs - Create jobs without artifact links
3. Artifacts - Register all artifacts
4. Link Artifacts - Link job inputs/outputs to artifacts
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class GitContext:
    """Git context for session registration."""

    repo: str | None
    commit: str | None
    branch: str | None


@dataclass
class SessionRegistrationResult:
    """Result of session registration."""

    success: bool
    session_hash: str
    session_url: str | None = None
    error: str | None = None


@dataclass
class ArtifactRegistrationResult:
    """Result of artifact registration."""

    success_count: int
    error_count: int
    errors: list[str]


@dataclass
class JobRegistrationResult:
    """Result of job registration."""

    success: bool
    job_uid: str
    job_id: str | None = None
    error: str | None = None


@dataclass
class JobLinkResult:
    """Result of job artifact linking."""

    success: bool
    job_uid: str
    inputs_linked: int = 0
    outputs_linked: int = 0
    error: str | None = None


@dataclass
class BatchRegistrationResult:
    """Result of batch lineage registration."""

    session_registered: bool
    jobs_created: int
    jobs_failed: int
    artifacts_registered: int
    artifacts_failed: int
    links_created: int
    links_failed: int
    errors: list[str]


@runtime_checkable
class ISessionRegistrar(Protocol):
    """Protocol for session registration operations."""

    def compute_session_hash(
        self,
        roar_dir: str,
        session_id: int | None,
        fallback_suffix: str | None = None,
    ) -> str:
        """Compute session hash from roar directory and session ID."""
        ...

    def register(
        self,
        session_hash: str,
        git_context: GitContext,
    ) -> SessionRegistrationResult:
        """Register session with GLaaS."""
        ...


@runtime_checkable
class IArtifactRegistrar(Protocol):
    """Protocol for artifact registration operations."""

    def register_single(
        self,
        hashes: list[dict[str, str]],
        size: int,
        source_type: str | None,
        session_hash: str,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> tuple[bool, str | None]:
        """Register a single artifact. Returns (success, error_message)."""
        ...

    def register_batch(
        self,
        artifacts: list[dict],
        session_hash: str,
    ) -> ArtifactRegistrationResult:
        """Register multiple artifacts in batch."""
        ...


@runtime_checkable
class IJobRegistrar(Protocol):
    """Protocol for job registration operations."""

    def create_job(
        self,
        command: str,
        timestamp: float,
        session_hash: str,
        job_uid: str,
        git_commit: str,
        git_branch: str,
        duration_seconds: float,
        exit_code: int,
        job_type: str | None,
        step_number: int,
        metadata: str | None = None,
    ) -> JobRegistrationResult:
        """Create a job WITHOUT artifact links."""
        ...

    def link_job_artifacts(
        self,
        session_hash: str,
        job_uid: str,
        inputs: list[dict[str, str]] | None,
        outputs: list[dict[str, str]] | None,
    ) -> JobLinkResult:
        """Link inputs/outputs to an existing job AFTER artifacts registered."""
        ...


@runtime_checkable
class IRegistrationCoordinator(Protocol):
    """
    Protocol for orchestrating registration in correct order.

    Enforces the 4-phase registration pattern:
    1. Session (already registered, passed as session_hash)
    2. Create all jobs (without I/O)
    3. Register all artifacts
    4. Link job I/O for each job
    """

    def register_lineage(
        self,
        session_hash: str,
        git_context: GitContext,
        jobs: list[dict],
        artifacts: list[dict],
    ) -> BatchRegistrationResult:
        """
        Register complete lineage with correct ordering.

        Args:
            session_hash: Pre-computed session hash (session already registered)
            git_context: Git context for the session
            jobs: List of job dicts with _inputs and _outputs for linking
            artifacts: List of artifact dicts to register

        Returns:
            BatchRegistrationResult with counts and errors
        """
        ...


@runtime_checkable
class ISecretFilter(Protocol):
    """Protocol for filtering secrets from registration data."""

    def filter_command(self, command: str) -> tuple[str, list[str]]:
        """Filter secrets from command. Returns (filtered, detection_ids)."""
        ...

    def filter_git_url(self, url: str) -> tuple[str, list[str]]:
        """Filter secrets from git URL. Returns (filtered, detection_ids)."""
        ...

    def filter_metadata(self, metadata: dict) -> tuple[dict, list[str]]:
        """Filter secrets from metadata dict. Returns (filtered, detection_ids)."""
        ...
