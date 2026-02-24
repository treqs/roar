"""
Job registration service.

Consolidates job registration logic from put.py and coordinator.py.
"""

import json
from functools import cached_property
from typing import Any

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import (
    GitContext,
    IJobRegistrar,
    ISecretFilter,
    JobLinkResult,
    JobRegistrationResult,
)
from ...core.logging import get_logger
from ...core.validation import validate_job_registration
from ...glaas_client import GlaasClient
from . import _artifact_ref

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

        self._logger = logger or get_logger()

    @cached_property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        return self._client or GlaasClient()

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

    def create_jobs_batch(
        self,
        jobs: list[dict],
        session_hash: str,
        git_context: GitContext,
    ) -> list[JobRegistrationResult]:
        """
        Create multiple jobs in a single batch request WITHOUT artifact links.

        Validates and filters each job, then sends all valid jobs in one
        ``register_jobs_batch`` call.  Jobs that fail validation are returned
        as individual failure results without hitting the server.

        Args:
            jobs: List of job dicts (same format as ``register_lineage`` jobs).
            session_hash: Session these jobs belong to.
            git_context: Fallback git context for jobs missing git fields.

        Returns:
            One ``JobRegistrationResult`` per input job, in the same order.
        """
        if not jobs:
            return []

        results: list[JobRegistrationResult | None] = [None] * len(jobs)
        payloads: list[dict] = []
        payload_indices: list[int] = []  # maps payload position → original index

        for i, job in enumerate(jobs):
            job_uid = job.get("job_uid")
            if not job_uid:
                self._logger.warning("Skipping job without job_uid")
                results[i] = JobRegistrationResult(
                    success=False, job_uid="", error="Job missing job_uid"
                )
                continue

            command = job.get("command", "")
            git_commit = job.get("git_commit") or git_context.commit or ""
            git_branch = job.get("git_branch") or git_context.branch or ""
            metadata = job.get("metadata")

            # Filter sensitive data
            filtered_command, _, filtered_metadata = self._filter_job_data(command, None, metadata)

            # Validate
            validation = validate_job_registration(
                command=filtered_command,
                timestamp=job.get("timestamp", 0.0),
                session_hash=session_hash,
                job_uid=job_uid,
                git_commit=git_commit,
                git_branch=git_branch,
                job_type=job.get("job_type"),
                step_number=job.get("step_number", 0),
            )
            if not validation:
                error_msg = "; ".join(validation.errors)
                self._logger.warning("Job validation failed for %s: %s", job_uid, error_msg)
                results[i] = JobRegistrationResult(success=False, job_uid=job_uid, error=error_msg)
                continue

            payload: dict = {
                "command": filtered_command,
                "timestamp": job.get("timestamp", 0.0),
                "job_uid": job_uid,
                "git_commit": git_commit,
                "git_branch": git_branch,
                "duration_seconds": job.get("duration_seconds", 0.0),
                "exit_code": job.get("exit_code", 0),
                "job_type": job.get("job_type") or "run",
                "step_number": job.get("step_number", 0),
            }
            if filtered_metadata:
                payload["metadata"] = filtered_metadata

            payloads.append(payload)
            payload_indices.append(i)

        if not payloads:
            return [r for r in results if r is not None]

        self._logger.debug(
            "Batch registering %d jobs under session %s", len(payloads), session_hash[:12]
        )

        job_ids, errors, overall_error = self.client.register_jobs_batch(
            session_hash=session_hash,
            jobs=payloads,
        )

        if overall_error:
            # Entire batch failed — mark all payload jobs as failed
            for idx in payload_indices:
                job_uid = jobs[idx].get("job_uid", "")
                results[idx] = JobRegistrationResult(
                    success=False, job_uid=job_uid, error=overall_error
                )
        else:
            for pos, idx in enumerate(payload_indices):
                job_uid = payloads[pos]["job_uid"]
                if pos < len(errors) and errors[pos]:
                    results[idx] = JobRegistrationResult(
                        success=False, job_uid=job_uid, error=errors[pos]
                    )
                else:
                    job_id = job_ids[pos] if pos < len(job_ids) else None
                    results[idx] = JobRegistrationResult(
                        success=True,
                        job_uid=job_uid,
                        job_id=str(job_id) if job_id else None,
                    )

        self._logger.debug(
            "Batch job registration complete: %d succeeded, %d failed",
            sum(1 for r in results if r and r.success),
            sum(1 for r in results if r and not r.success),
        )

        return [r for r in results if r is not None]

    def link_job_artifacts(
        self,
        session_hash: str,
        job_uid: str,
        inputs: list[dict[str, Any]] | None,
        outputs: list[dict[str, Any]] | None,
    ) -> JobLinkResult:
        """
        Link inputs/outputs to an existing job using session-scoped endpoints.

        This is phase 4 of the 4-phase registration:
        Called AFTER artifacts have been registered (phase 3).

        Args:
            session_hash: Session this job belongs to
            job_uid: Job UID to link artifacts to
            inputs: List of {artifact_hash, path, byte_ranges} dicts for inputs
            outputs: List of {artifact_hash, path, byte_ranges} dicts for outputs

        Returns:
            JobLinkResult with counts of linked artifacts
        """
        valid_inputs = self._normalize_link_artifacts(inputs or [], "input")
        valid_outputs = self._normalize_link_artifacts(outputs or [], "output")

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

    def _normalize_link_artifacts(
        self,
        items: list[dict[str, Any]],
        artifact_kind: str,
    ) -> list[dict[str, Any]]:
        """Normalize link artifacts and keep only entries with artifact hash + path."""
        valid_items: list[dict[str, Any]] = []

        for item in items:
            path = item.get("path")
            if not path:
                ref_preview = _artifact_ref.preview(item)
                if ref_preview:
                    self._logger.warning(
                        "Dropping %s %s: missing path",
                        artifact_kind,
                        ref_preview,
                    )
                else:
                    self._logger.warning("Dropping %s artifact: missing path", artifact_kind)
                continue

            artifact_hash = item.get("artifact_hash") or item.get("artifact_id")
            if not artifact_hash:
                self._logger.warning(
                    "Dropping %s artifact for path %s: missing artifact_hash",
                    artifact_kind,
                    path,
                )
                continue

            normalized: dict[str, Any] = {
                "artifact_hash": artifact_hash,
                "path": path,
            }
            if item.get("byte_ranges") is not None:
                normalized["byte_ranges"] = item["byte_ranges"]

            valid_items.append(normalized)

        return valid_items
