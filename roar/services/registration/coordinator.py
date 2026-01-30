"""
Registration coordinator service.

Orchestrates the 4-phase registration pattern:
1. Session - Register session with git context
2. Jobs - Create jobs without artifact links
3. Artifacts - Register all artifacts
4. Link Artifacts - Link job inputs/outputs to artifacts
"""

from ...core.di import resolve_or_default
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import (
    BatchRegistrationResult,
    GitContext,
    IRegistrationCoordinator,
)
from .artifact import ArtifactRegistrationService
from .job import JobRegistrationService
from .session import SessionRegistrationService


class RegistrationCoordinator(IRegistrationCoordinator):
    """
    Coordinates registration in the correct order.

    This service enforces the 4-phase registration pattern to ensure
    FK constraints are satisfied:
    1. Session already registered (passed as session_hash)
    2. Create all jobs (without I/O)
    3. Register all artifacts
    4. Link job I/O for each job

    Replaces the inline registration logic in put.py:391-572.
    """

    def __init__(
        self,
        session_service: SessionRegistrationService | None = None,
        artifact_service: ArtifactRegistrationService | None = None,
        job_service: JobRegistrationService | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the registration coordinator.

        Args:
            session_service: Session registration service
            artifact_service: Artifact registration service
            job_service: Job registration service
            logger: Logger instance. If None, resolves from DI container.
        """
        self._session_service = session_service
        self._artifact_service = artifact_service
        self._job_service = job_service
        from ...services.logging import NullLogger

        self._logger = logger or resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]

    @property
    def session_service(self) -> SessionRegistrationService:
        """Get or create session service."""
        if self._session_service is None:
            self._session_service = SessionRegistrationService()
        return self._session_service

    @property
    def artifact_service(self) -> ArtifactRegistrationService:
        """Get or create artifact service."""
        if self._artifact_service is None:
            self._artifact_service = ArtifactRegistrationService()
        return self._artifact_service

    @property
    def job_service(self) -> JobRegistrationService:
        """Get or create job service."""
        if self._job_service is None:
            self._job_service = JobRegistrationService()
        return self._job_service

    def register_lineage(
        self,
        session_hash: str,
        git_context: GitContext,
        jobs: list[dict],
        artifacts: list[dict],
    ) -> BatchRegistrationResult:
        """
        Register complete lineage with correct ordering.

        Implements the 4-phase registration pattern:
        1. Session already registered (passed as session_hash)
        2. Create all jobs (without I/O) - Phase 2
        3. Register all artifacts - Phase 3
        4. Link job I/O for each job - Phase 4

        Args:
            session_hash: Pre-computed session hash (session already registered)
            git_context: Git context for the session
            jobs: List of job dicts with _inputs and _outputs for linking
            artifacts: List of artifact dicts to register

        Returns:
            BatchRegistrationResult with counts and errors
        """
        errors: list[str] = []
        jobs_created = 0
        jobs_failed = 0
        artifacts_registered = 0
        artifacts_failed = 0
        links_created = 0
        links_failed = 0

        self._logger.debug(
            "Starting lineage registration: session=%s, jobs=%d, artifacts=%d",
            session_hash[:12],
            len(jobs),
            len(artifacts),
        )

        # Phase 2: Create all jobs WITHOUT artifact links
        self._logger.debug("Phase 2: Creating %d jobs without artifact links", len(jobs))
        job_uids_created = []

        for job in jobs:
            job_uid = job.get("job_uid")
            if not job_uid:
                self._logger.warning("Skipping job without job_uid")
                jobs_failed += 1
                errors.append("Job missing job_uid")
                continue

            result = self.job_service.create_job(
                command=job.get("command", ""),
                timestamp=job.get("timestamp", 0.0),
                session_hash=session_hash,
                job_uid=job_uid,
                git_commit=job.get("git_commit") or git_context.commit or "",
                git_branch=job.get("git_branch") or git_context.branch or "",
                duration_seconds=job.get("duration_seconds", 0.0),
                exit_code=job.get("exit_code", 0),
                job_type=job.get("job_type") or "run",  # Normalize None to "run"
                step_number=job.get("step_number", 0),
                metadata=job.get("metadata"),
            )

            if result.success:
                jobs_created += 1
                job_uids_created.append(job_uid)
            else:
                jobs_failed += 1
                if result.error:
                    errors.append(f"Job {job_uid}: {result.error}")

        self._logger.debug(
            "Phase 2 complete: %d jobs created, %d failed",
            jobs_created,
            jobs_failed,
        )

        # Phase 3: Register all artifacts
        self._logger.debug("Phase 3: Registering %d artifacts", len(artifacts))

        if artifacts:
            art_result = self.artifact_service.register_batch(artifacts, session_hash)
            artifacts_registered = art_result.success_count
            artifacts_failed = art_result.error_count
            errors.extend(art_result.errors)

        self._logger.debug(
            "Phase 3 complete: %d artifacts registered, %d failed",
            artifacts_registered,
            artifacts_failed,
        )

        # Phase 4: Link job artifacts
        self._logger.debug("Phase 4: Linking artifacts to %d jobs", len(job_uids_created))

        for job in jobs:
            job_uid = job.get("job_uid")
            if not job_uid or job_uid not in job_uids_created:
                continue

            # Get inputs/outputs in {hash, path} format
            inputs = self._extract_io_list(job, "_inputs", "_input_hashes")
            outputs = self._extract_io_list(job, "_outputs", "_output_hashes")

            if not inputs and not outputs:
                continue

            link_result = self.job_service.link_job_artifacts(
                session_hash=session_hash,
                job_uid=job_uid,
                inputs=inputs,
                outputs=outputs,
            )

            if link_result.success:
                links_created += link_result.inputs_linked + link_result.outputs_linked
            else:
                links_failed += 1
                if link_result.error:
                    errors.append(f"Link {job_uid}: {link_result.error}")

        self._logger.debug(
            "Phase 4 complete: %d links created, %d failed",
            links_created,
            links_failed,
        )

        self._logger.debug(
            "Lineage registration complete: jobs=%d/%d, artifacts=%d/%d, links=%d",
            jobs_created,
            jobs_created + jobs_failed,
            artifacts_registered,
            artifacts_registered + artifacts_failed,
            links_created,
        )

        return BatchRegistrationResult(
            session_registered=True,  # Session was already registered
            jobs_created=jobs_created,
            jobs_failed=jobs_failed,
            artifacts_registered=artifacts_registered,
            artifacts_failed=artifacts_failed,
            links_created=links_created,
            links_failed=links_failed,
            errors=errors,
        )

    def _extract_io_list(
        self,
        job: dict,
        structured_key: str,
        hash_list_key: str,
    ) -> list[dict[str, str]]:
        """
        Extract I/O list from job dict.

        Handles both structured format (_inputs/_outputs) and hash-only format
        (_input_hashes/_output_hashes).

        Args:
            job: Job dict
            structured_key: Key for structured I/O list (e.g., "_inputs")
            hash_list_key: Key for hash-only list (e.g., "_input_hashes")

        Returns:
            List of {hash, path} dicts
        """
        # Prefer structured format with path
        structured = job.get(structured_key, [])
        if structured:
            result = []
            for item in structured:
                h = item.get("hash")
                p = item.get("path")
                if h and p:
                    result.append({"hash": h, "path": p})
                elif h:
                    self._logger.warning("Dropping I/O item %s: missing path", h[:12])
            return result

        # Fallback to hash-only format (path will be empty)
        hash_list = job.get(hash_list_key, [])
        if hash_list:
            return [{"hash": h, "path": ""} for h in hash_list if h]

        return []

    def register_uploaded_artifacts(
        self,
        artifacts: list[tuple[str, int, str, str]],
        session_hash: str,
        scheme: str,
        dest_url: str,
        is_dir: bool,
    ) -> tuple[int, list[str]]:
        """
        Register uploaded artifacts (convenience method for put command).

        Args:
            artifacts: List of (hash, size, path, rel_path) tuples
            session_hash: Session hash
            scheme: Cloud storage scheme (s3, gs)
            dest_url: Destination URL
            is_dir: Whether destination is a directory

        Returns:
            Tuple of (registered_count, errors)
        """
        errors = []
        registered = 0

        for file_hash, size, _path, rel_path in artifacts:
            if is_dir:
                file_url = f"{dest_url.rstrip('/')}/{rel_path}"
            else:
                file_url = dest_url

            success, error = self.artifact_service.register_single(
                hashes=[{"algorithm": "blake3", "digest": file_hash}],
                size=size,
                source_type=scheme,
                session_hash=session_hash,
                source_url=file_url,
            )

            if success:
                registered += 1
            elif error:
                errors.append(f"{file_hash[:12]}: {error}")

        return registered, errors
