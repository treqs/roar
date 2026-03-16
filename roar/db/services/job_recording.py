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
    - Batch filtering/hash computation for input/output files
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
        job_uid: str | None = None,
        git_repo: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        input_files: list[str] | None = None,
        output_files: list[str] | None = None,
        metadata: str | None = None,
        execution_backend: str | None = None,
        execution_role: str | None = None,
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
            job_uid: Optional deterministic job UID to persist
            git_repo: Repository URL or path
            git_commit: Git commit hash
            git_branch: Git branch name
            duration_seconds: Execution duration
            exit_code: Process exit code
            input_files: List of input file paths
            output_files: List of output file paths
            metadata: JSON metadata string
            execution_backend: Execution backend that owns the job semantics
            execution_role: Backend-defined role for the job
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

        input_candidates = input_files or []
        output_candidates = output_files or []
        all_paths = self._unique_paths([*input_candidates, *output_candidates])

        # Always include blake3 for hashability checks used by lineage lookups.
        batch_algorithms = list(hash_algorithms)
        if "blake3" not in batch_algorithms:
            batch_algorithms.append("blake3")

        hashes_by_path = self._hashing_service.compute_hashes_batch(all_paths, batch_algorithms)
        hashable_inputs = [
            path
            for path in input_candidates
            if path in hashes_by_path and "blake3" in hashes_by_path[path]
        ]
        hashable_outputs = [
            path
            for path in output_candidates
            if path in hashes_by_path and "blake3" in hashes_by_path[path]
        ]

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
            job_uid=job_uid,
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
            execution_backend=execution_backend,
            execution_role=execution_role,
            job_type=job_type,
            telemetry=telemetry,
        )

        # Register and link input artifacts
        self._register_artifacts(
            job_id,
            hashable_inputs,
            hashes_by_path,
            hash_algorithms,
            is_input=True,
        )

        # Register and link output artifacts
        self._register_artifacts(
            job_id,
            hashable_outputs,
            hashes_by_path,
            hash_algorithms,
            is_input=False,
        )

        # Commit transaction
        self._session.commit()

        # Update session hash if assigned
        if session_id:
            self._session_repo.update_hash(session_id, self._job_repo)

        return job_id, job_uid

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
        hashes_by_path: dict[str, dict[str, str]],
        hash_algorithms: list[str],
        is_input: bool,
    ) -> None:
        """Register artifacts and link them to the job."""
        # Batch-check which paths already have edges for this job
        if is_input:
            already_linked = self._job_repo.existing_input_paths(job_id, file_paths)
        else:
            already_linked = self._job_repo.existing_output_paths(job_id, file_paths)

        # Build batch items, skipping paths that already have edges
        batch_items: list[tuple[dict[str, str], int, str | None]] = []
        valid_paths: list[str] = []
        for path in file_paths:
            if path in already_linked:
                continue

            path_hashes = hashes_by_path.get(path)
            if not path_hashes:
                continue
            hashes = {algo: digest for algo in hash_algorithms if (digest := path_hashes.get(algo))}
            if not hashes:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            batch_items.append((hashes, size, path))
            valid_paths.append(path)

        if not batch_items:
            return

        # Batch register artifacts
        artifact_ids = self._artifact_repo.register_batch(batch_items)

        # Batch create edges
        edges = list(zip(artifact_ids, valid_paths))
        if is_input:
            self._job_repo.add_inputs_batch(job_id, edges)
        else:
            self._job_repo.add_outputs_batch(job_id, edges)

    @staticmethod
    def _unique_paths(paths: list[str]) -> list[str]:
        """Return unique paths preserving input order."""
        unique: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path not in seen:
                seen.add(path)
                unique.append(path)
        return unique
