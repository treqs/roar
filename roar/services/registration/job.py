"""
Job registration service.

Consolidates job registration logic from put.py and coordinator.py.
"""

import json

from ...core.di import resolve_or_default
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import (
    IJobRegistrar,
    ISecretFilter,
    JobLinkResult,
    JobRegistrationResult,
)
from ...core.validation import validate_job_registration
from ...glaas_client import GlaasClient

# Maximum artifacts per request to avoid exceeding server body-parser limits (~100KB-1MB)
MAX_ARTIFACTS_PER_REQUEST = 100


def _batch_artifacts(artifacts: list[dict], batch_size: int) -> list[list[dict]]:
    """Split artifacts into batches of at most batch_size."""
    return [artifacts[i : i + batch_size] for i in range(0, len(artifacts), batch_size)]


class JobRegistrationService(IJobRegistrar):
    """
    Service for job registration operations.

    Consolidates the duplicated job registration logic from:
    - put.py:470-572 (batch job registration)
    - coordinator.py:277-395 (live job creation and completion)

    Follows the 4-phase registration pattern:
    1. create_job() - Creates job WITHOUT artifact links
    2. link_job_artifacts() - Links I/O AFTER artifacts registered
    """

    def __init__(
        self,
        client: GlaasClient | None = None,
        secret_filter: ISecretFilter | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the job registration service.

        Args:
            client: GLaaS client for server communication. If None, creates one.
            secret_filter: Optional secret filter for redacting sensitive data.
            logger: Logger instance. If None, resolves from DI container.
        """
        self._client = client
        self._secret_filter = secret_filter
        from ...services.logging import NullLogger

        self._logger = logger or resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]

    @property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        if self._client is None:
            self._client = GlaasClient()
        return self._client

    def _filter_job_data(
        self,
        command: str,
        git_repo: str | None,
        metadata: str | None,
    ) -> tuple[str, str | None, str | None]:
        """
        Filter sensitive data from job fields.

        Consolidates the secret filtering logic from put.py:483-504.

        Args:
            command: Raw command string
            git_repo: Raw git repository URL
            metadata: Raw metadata JSON string

        Returns:
            Tuple of (filtered_command, filtered_git_repo, filtered_metadata)
        """
        if not self._secret_filter:
            return command, git_repo, metadata

        filtered_command, _ = self._secret_filter.filter_command(command)

        filtered_git_repo = git_repo
        if git_repo:
            filtered_git_repo, _ = self._secret_filter.filter_git_url(git_repo)

        filtered_metadata = metadata
        if metadata:
            try:
                meta_dict = json.loads(metadata)
                filtered_meta_dict, _ = self._secret_filter.filter_metadata(meta_dict)
                filtered_metadata = json.dumps(filtered_meta_dict)
            except (json.JSONDecodeError, TypeError):
                # Keep original if not valid JSON
                pass

        return filtered_command, filtered_git_repo, filtered_metadata

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
        """
        Create a job WITHOUT artifact links.

        This is phase 2 of the 4-phase registration:
        Jobs are created first, then artifacts registered, then links created.

        Args:
            command: Command that was executed
            timestamp: Unix timestamp of job start
            session_hash: Session this job belongs to
            job_uid: Unique job identifier
            git_commit: Git commit SHA
            git_branch: Git branch name
            duration_seconds: Job duration
            exit_code: Process exit code
            job_type: Type of job (None for run, "build" for build)
            step_number: Step number in session
            metadata: Optional JSON metadata

        Returns:
            JobRegistrationResult with success status
        """
        # Filter sensitive data
        filtered_command, _, filtered_metadata = self._filter_job_data(command, None, metadata)

        # Validate job data
        validation = validate_job_registration(
            command=filtered_command,
            timestamp=timestamp,
            session_hash=session_hash,
            job_uid=job_uid,
            git_commit=git_commit,
            git_branch=git_branch,
            job_type=job_type,
            step_number=step_number,
        )
        if not validation:
            error_msg = "; ".join(validation.errors)
            self._logger.warning("Job validation failed for %s: %s", job_uid, error_msg)
            return JobRegistrationResult(
                success=False,
                job_uid=job_uid,
                error=error_msg,
            )

        # Register job using session-scoped endpoint (no inputs/outputs - linked separately)
        job_id, error = self.client.register_job(
            session_hash=session_hash,
            command=filtered_command,
            timestamp=timestamp,
            job_uid=job_uid,
            git_commit=git_commit,
            git_branch=git_branch,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            job_type=job_type,
            step_number=step_number,
            metadata=filtered_metadata,
        )

        if error:
            self._logger.debug("Job creation failed for %s: %s", job_uid, error)
            return JobRegistrationResult(
                success=False,
                job_uid=job_uid,
                error=error,
            )

        self._logger.debug("Job created: %s -> server_id=%s", job_uid, job_id)
        return JobRegistrationResult(
            success=True,
            job_uid=job_uid,
            job_id=str(job_id) if job_id else None,
        )

    def link_job_artifacts(
        self,
        session_hash: str,
        job_uid: str,
        inputs: list[dict[str, str]] | None,
        outputs: list[dict[str, str]] | None,
    ) -> JobLinkResult:
        """
        Link inputs/outputs to an existing job using session-scoped endpoints.

        This is phase 4 of the 4-phase registration:
        Called AFTER artifacts have been registered (phase 3).

        Args:
            session_hash: Session this job belongs to
            job_uid: Job UID to link artifacts to
            inputs: List of {hash, path, size, source_type, metadata} dicts for inputs
            outputs: List of {hash, path, size, source_type, metadata} dicts for outputs

        Returns:
            JobLinkResult with counts of linked artifacts
        """
        # Filter to only include items with valid data
        valid_inputs = []
        for item in inputs or []:
            if item.get("hash") and item.get("path"):
                valid_inputs.append(item)
            elif item.get("hash"):
                self._logger.warning("Dropping input %s: missing path", item["hash"][:12])

        valid_outputs = []
        for item in outputs or []:
            if item.get("hash") and item.get("path"):
                valid_outputs.append(item)
            elif item.get("hash"):
                self._logger.warning("Dropping output %s: missing path", item["hash"][:12])

        if not valid_inputs and not valid_outputs:
            self._logger.debug("No artifacts to link for job %s", job_uid)
            return JobLinkResult(
                success=True,
                job_uid=job_uid,
                inputs_linked=0,
                outputs_linked=0,
            )

        self._logger.debug(
            "Linking artifacts to job %s: %d inputs, %d outputs",
            job_uid,
            len(valid_inputs),
            len(valid_outputs),
        )

        inputs_linked = 0
        outputs_linked = 0
        errors = []

        # Call separate endpoints for inputs and outputs, batching to avoid payload limits
        if valid_inputs:
            input_batches = _batch_artifacts(valid_inputs, MAX_ARTIFACTS_PER_REQUEST)
            for batch_idx, batch in enumerate(input_batches):
                self._logger.debug(
                    "Sending input batch %d/%d for job %s: %d artifacts",
                    batch_idx + 1,
                    len(input_batches),
                    job_uid,
                    len(batch),
                )
                result, error = self.client.register_job_inputs(
                    session_hash=session_hash,
                    job_uid=job_uid,
                    artifacts=batch,
                )
                if error:
                    self._logger.debug("Input linking failed for %s: %s", job_uid, error)
                    errors.append(f"inputs: {error}")
                    break  # Stop on first error
                inputs_linked += result.get("inputs_linked", len(batch)) if result else len(batch)

        if valid_outputs:
            output_batches = _batch_artifacts(valid_outputs, MAX_ARTIFACTS_PER_REQUEST)
            for batch_idx, batch in enumerate(output_batches):
                self._logger.debug(
                    "Sending output batch %d/%d for job %s: %d artifacts",
                    batch_idx + 1,
                    len(output_batches),
                    job_uid,
                    len(batch),
                )
                result, error = self.client.register_job_outputs(
                    session_hash=session_hash,
                    job_uid=job_uid,
                    artifacts=batch,
                )
                if error:
                    self._logger.debug("Output linking failed for %s: %s", job_uid, error)
                    errors.append(f"outputs: {error}")
                    break  # Stop on first error
                outputs_linked += result.get("outputs_linked", len(batch)) if result else len(batch)

        if errors:
            return JobLinkResult(
                success=False,
                job_uid=job_uid,
                inputs_linked=inputs_linked,
                outputs_linked=outputs_linked,
                error="; ".join(errors),
            )

        self._logger.debug(
            "Linked artifacts to job %s: %d inputs, %d outputs",
            job_uid,
            inputs_linked,
            outputs_linked,
        )

        return JobLinkResult(
            success=True,
            job_uid=job_uid,
            inputs_linked=inputs_linked,
            outputs_linked=outputs_linked,
        )
