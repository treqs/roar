"""
Job recording service.

Encapsulates the business logic for recording jobs with their inputs,
outputs, and session associations.
"""

import os

from sqlalchemy.orm import Session as SASession

from ..repositories import (
    SQLAlchemyArtifactRepository,
    SQLAlchemyJobRepository,
    SQLAlchemySessionRepository,
)
from .hashing import DefaultHashingService
from .session import DefaultSessionService


class JobRecordingService:
    """
    Service for recording job executions with full lineage tracking.

    Handles:
    - Filtering hashable input/output files
    - Computing step identity for deduplication
    - Session assignment and step numbering
    - Artifact registration and linking
    - Transaction management
    """

    def __init__(
        self,
        session: SASession,
        job_repo: SQLAlchemyJobRepository,
        artifact_repo: SQLAlchemyArtifactRepository,
        session_repo: SQLAlchemySessionRepository,
        hashing_service: DefaultHashingService,
        session_service: DefaultSessionService,
    ):
        """
        Initialize JobRecordingService.

        Args:
            session: SQLAlchemy session (for transaction management)
            job_repo: Job repository
            artifact_repo: Artifact repository
            session_repo: Session repository
            hashing_service: Service for computing file hashes
            session_service: Service for session operations
        """
        self._session = session
        self._job_repo = job_repo
        self._artifact_repo = artifact_repo
        self._session_repo = session_repo
        self._hashing_service = hashing_service
        self._session_service = session_service

    def record_job(
        self,
        command: str,
        timestamp: float,
        git_repo: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        input_files: list[str] | None = None,
        output_files: list[str] | None = None,
        metadata: str | None = None,
        step_name: str | None = None,
        assign_to_session: bool = True,
        job_type: str | None = None,
        repo_root: str | None = None,
        telemetry: str | None = None,
        hash_algorithms: list[str] | None = None,
    ) -> tuple[int, str]:
        """
        Record a job with its inputs and outputs.

        Args:
            command: Full command string that was executed
            timestamp: Job start time (Unix timestamp)
            git_repo: Repository URL or path
            git_commit: Git commit hash
            git_branch: Git branch name
            duration_seconds: Execution duration
            exit_code: Process exit code
            input_files: List of input file paths
            output_files: List of output file paths
            metadata: JSON metadata string
            step_name: User-assigned step name
            assign_to_session: Whether to assign to active session
            job_type: Job type ('run', 'build', etc.)
            repo_root: Repository root path for path normalization
            telemetry: JSON telemetry data (external service links)
            hash_algorithms: Hash algorithms to use (default: ['blake3'])

        Returns:
            Tuple of (job_id, job_uid)
        """
        if hash_algorithms is None:
            hash_algorithms = ["blake3"]

        # Filter to only files that can be hashed
        hashable_inputs = self._filter_hashable_files(input_files)
        hashable_outputs = self._filter_hashable_files(output_files)

        # Compute step identity for deduplication
        step_identity = self._session_service.compute_step_identity(
            hashable_inputs, hashable_outputs, repo_root, command
        )

        # Handle session assignment
        session_id, step_number = self._assign_to_session(
            assign_to_session, step_identity, git_commit
        )

        # Create the job record
        job_id, job_uid = self._job_repo.create(
            command=command,
            timestamp=timestamp,
            step_identity=step_identity,
            session_id=session_id,
            step_number=step_number,
            step_name=step_name,
            git_repo=git_repo,
            git_commit=git_commit,
            git_branch=git_branch,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            metadata=metadata,
            job_type=job_type,
            telemetry=telemetry,
        )

        # Register and link input artifacts
        self._register_artifacts(job_id, hashable_inputs, hash_algorithms, is_input=True)

        # Register and link output artifacts
        self._register_artifacts(job_id, hashable_outputs, hash_algorithms, is_input=False)

        # Commit transaction
        self._session.commit()

        # Update session hash if assigned
        if session_id:
            self._session_repo.update_hash(session_id, self._job_repo)

        return job_id, job_uid

    def _filter_hashable_files(self, files: list[str] | None) -> list[str]:
        """Filter files to only those that can be hashed."""
        if not files:
            return []

        hashable = []
        for path in files:
            file_hash = self._hashing_service.compute_hash(path, "blake3")
            if file_hash:
                hashable.append(path)
        return hashable

    def _assign_to_session(
        self,
        assign: bool,
        step_identity: str,
        git_commit: str | None,
    ) -> tuple[int | None, int | None]:
        """Handle session assignment and step numbering."""
        if not assign:
            return None, None

        session_id = self._session_repo.get_or_create_active()

        # Check for existing step with same identity
        existing_step = self._session_repo.get_step_by_identity(session_id, step_identity)
        if existing_step:
            step_number = existing_step["step_number"]
        else:
            step_number = self._session_repo.get_next_step_number(session_id)

        self._session_repo.update_current_step(session_id, step_number)

        if git_commit:
            self._session_repo.update_git_commits(session_id, git_commit, update_start=True)

        return session_id, step_number

    def _register_artifacts(
        self,
        job_id: int,
        file_paths: list[str],
        hash_algorithms: list[str],
        is_input: bool,
    ) -> None:
        """Register artifacts and link them to the job."""
        for path in file_paths:
            hashes = self._hashing_service.compute_hashes(path, hash_algorithms)
            if hashes:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0

                artifact_id, _ = self._artifact_repo.register(hashes, size, path)

                if is_input:
                    self._job_repo.add_input(job_id, artifact_id, path)
                else:
                    self._job_repo.add_output(job_id, artifact_id, path)
