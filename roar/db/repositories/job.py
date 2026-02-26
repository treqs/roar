"""
SQLAlchemy job repository implementation.

Handles job recording and retrieval operations.
"""

import json
import os
import secrets
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ...core.interfaces.logger import ILogger
from ...core.interfaces.repositories import ArtifactRepository, JobRepository
from ...core.logging import get_logger
from ..models import Artifact, CollectionMember, Job, JobInput, JobOutput


class SQLAlchemyJobRepository(JobRepository):
    """
    SQLAlchemy implementation of job repository.

    Manages job records and their input/output artifact associations.
    """

    def __init__(
        self,
        session: Session,
        artifact_repository: ArtifactRepository,
        logger: ILogger | None = None,
    ):
        """
        Initialize repository with database session and artifact repository.

        Args:
            session: SQLAlchemy session
            artifact_repository: Repository for artifact lookups (injected)
            logger: Logger instance. If None, resolves from DI container.
        """
        self._session = session
        self._artifact_repository = artifact_repository
        self._logger = logger or get_logger()

    @staticmethod
    def _extract_script(command: str) -> str | None:
        """
        Extract the primary script name from a command.

        Args:
            command: Full command string

        Returns:
            Script name or None if not identifiable.
        """
        parts = command.split()
        for part in parts:
            if part.endswith(".py") or part.endswith(".sh"):
                return os.path.basename(part)
            if part == "-m" and len(parts) > parts.index(part) + 1:
                return parts[parts.index(part) + 1]
        return None

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
        """
        Create a job record.

        Args:
            command: Command that was executed
            timestamp: Unix timestamp when job started
            step_identity: Hash identifying this step in the session
            session_id: Session this job belongs to
            step_number: Step number within the session
            step_name: User-assigned step name
            git_repo: Git repository URL
            git_commit: Git commit hash
            git_branch: Git branch name
            duration_seconds: How long the job ran
            exit_code: Process exit code
            metadata: JSON metadata string
            job_type: Type of job ('run', 'build')
            telemetry: JSON telemetry data

        Returns:
            (job_id, job_uid) tuple.
        """
        script = self._extract_script(command)
        job_uid = secrets.token_hex(4)

        job = Job(
            job_uid=job_uid,
            timestamp=timestamp,
            command=command,
            script=script,
            step_identity=step_identity,
            session_id=session_id,
            step_number=step_number,
            step_name=step_name,
            git_repo=git_repo,
            git_commit=git_commit,
            git_branch=git_branch,
            duration_seconds=duration_seconds,
            exit_code=exit_code,
            metadata_=metadata,
            job_type=job_type,
            telemetry=telemetry,
        )
        self._session.add(job)
        self._session.flush()
        return job.id, job_uid

    def get(self, job_id: int) -> dict[str, Any] | None:
        """
        Get a job by ID.

        Args:
            job_id: Job database ID

        Returns:
            Job dict or None if not found.
        """
        job = self._session.get(Job, job_id)
        return self._job_to_dict(job) if job else None

    def get_by_uid(self, job_uid: str) -> dict[str, Any] | None:
        """
        Get a job by UID (exact match or prefix).

        Args:
            job_uid: Job UID or prefix (minimum 4 chars)

        Returns:
            Job dict or None if not found or ambiguous.
        """
        # Try exact match first
        job = self._session.execute(select(Job).where(Job.job_uid == job_uid)).scalar_one_or_none()
        if job:
            return self._job_to_dict(job)

        # Try prefix match if UID is at least 4 chars
        if len(job_uid) >= 4:
            jobs = (
                self._session.execute(select(Job).where(Job.job_uid.like(job_uid + "%")).limit(2))
                .scalars()
                .all()
            )
            if len(jobs) == 1:
                return self._job_to_dict(jobs[0])

        return None

    def update_metadata(self, job_id: int, metadata: str | None) -> None:
        """Update metadata JSON for a job."""
        job = self._session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        job.metadata_ = metadata
        self._session.flush()

    def add_input(
        self, job_id: int, artifact_id: str, path: str, byte_ranges: list[list[int]] | None = None
    ) -> None:
        """
        Record an input artifact for a job.

        If the same (job_id, artifact_id, path) already exists and byte_ranges
        are provided, the new ranges are accumulated into the existing row.

        Args:
            job_id: Job database ID
            artifact_id: Artifact UUID
            path: File path where artifact was read
            byte_ranges: Optional list of [start, end] byte range tuples
        """
        existing = self._session.execute(
            select(JobInput).where(
                JobInput.job_id == job_id,
                JobInput.artifact_id == artifact_id,
                JobInput.path == path,
            )
        ).scalar_one_or_none()
        if existing:
            if byte_ranges:
                current = json.loads(existing.byte_ranges) if existing.byte_ranges else []
                current.extend(byte_ranges)
                existing.byte_ranges = json.dumps(current)
                self._session.flush()
        else:
            job_input = JobInput(
                job_id=job_id,
                artifact_id=artifact_id,
                path=path,
                byte_ranges=json.dumps(byte_ranges) if byte_ranges else None,
            )
            self._session.add(job_input)
            self._session.flush()

    def add_output(
        self, job_id: int, artifact_id: str, path: str, byte_ranges: list[list[int]] | None = None
    ) -> None:
        """
        Record an output artifact for a job.

        If the same (job_id, artifact_id, path) already exists and byte_ranges
        are provided, the new ranges are accumulated into the existing row.

        Args:
            job_id: Job database ID
            artifact_id: Artifact UUID
            path: File path where artifact was written
            byte_ranges: Optional list of [start, end] byte range tuples
        """
        existing = self._session.execute(
            select(JobOutput).where(
                JobOutput.job_id == job_id,
                JobOutput.artifact_id == artifact_id,
                JobOutput.path == path,
            )
        ).scalar_one_or_none()
        if existing:
            if byte_ranges:
                current = json.loads(existing.byte_ranges) if existing.byte_ranges else []
                current.extend(byte_ranges)
                existing.byte_ranges = json.dumps(current)
                self._session.flush()
        else:
            job_output = JobOutput(
                job_id=job_id,
                artifact_id=artifact_id,
                path=path,
                byte_ranges=json.dumps(byte_ranges) if byte_ranges else None,
            )
            self._session.add(job_output)
            self._session.flush()

    def get_inputs(self, job_id: int) -> list[dict[str, Any]]:
        """
        Get input artifacts for a job.

        Args:
            job_id: Job database ID

        Returns:
            List of input dicts with path, artifact_id, size, hashes, byte_ranges, and first_seen_path.
        """
        query = (
            select(
                JobInput.path,
                JobInput.artifact_id,
                JobInput.byte_ranges,
                Artifact.size,
                Artifact.first_seen_path,
                Artifact.kind,
                Artifact.component_count,
            )
            .join(Artifact, JobInput.artifact_id == Artifact.id)
            .where(JobInput.job_id == job_id)
        )
        rows = self._session.execute(query).all()

        results = []
        for path, artifact_id, byte_ranges, size, first_seen_path, kind, component_count in rows:
            hashes = self._artifact_repository.get_hashes(artifact_id)
            results.append(
                {
                    "path": path or first_seen_path,  # Use artifact path as fallback
                    "artifact_id": artifact_id,
                    "size": size,
                    "kind": kind,
                    "component_count": component_count,
                    "hashes": hashes,
                    # Backward compatibility: artifact_hash is the primary hash digest
                    "artifact_hash": hashes[0]["digest"] if hashes else None,
                    "first_seen_path": first_seen_path,
                    "byte_ranges": json.loads(byte_ranges) if byte_ranges else None,
                }
            )
        return results

    def get_outputs(self, job_id: int) -> list[dict[str, Any]]:
        """
        Get output artifacts for a job.

        Args:
            job_id: Job database ID

        Returns:
            List of output dicts with path, artifact_id, size, hashes, artifact_hash, byte_ranges, and first_seen_path.
        """
        query = (
            select(
                JobOutput.path,
                JobOutput.artifact_id,
                JobOutput.byte_ranges,
                Artifact.size,
                Artifact.first_seen_path,
                Artifact.kind,
                Artifact.component_count,
            )
            .join(Artifact, JobOutput.artifact_id == Artifact.id)
            .where(JobOutput.job_id == job_id)
        )
        rows = self._session.execute(query).all()

        results = []
        for path, artifact_id, byte_ranges, size, first_seen_path, kind, component_count in rows:
            hashes = self._artifact_repository.get_hashes(artifact_id)
            results.append(
                {
                    "path": path or first_seen_path,  # Use artifact path as fallback
                    "artifact_id": artifact_id,
                    "size": size,
                    "kind": kind,
                    "component_count": component_count,
                    "hashes": hashes,
                    # Backward compatibility: artifact_hash is the primary hash digest
                    "artifact_hash": hashes[0]["digest"] if hashes else None,
                    "first_seen_path": first_seen_path,
                    "byte_ranges": json.loads(byte_ranges) if byte_ranges else None,
                }
            )
        return results

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get most recent jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts, most recent first.
        """
        jobs = (
            self._session.execute(select(Job).order_by(Job.timestamp.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [self._job_to_dict(j) for j in jobs]

    def get_by_session(self, session_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get jobs for a specific session.

        Args:
            session_id: Session database ID
            limit: Maximum number of jobs to return

        Returns:
            List of job dicts, most recent first.
        """
        jobs = (
            self._session.execute(
                select(Job)
                .where(Job.session_id == session_id)
                .order_by(Job.timestamp.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [self._job_to_dict(j) for j in jobs]

    def get_latest_build_jobs(self, session_id: int) -> list[dict[str, Any]]:
        """
        Get latest build jobs by step number for a session.

        Args:
            session_id: Session database ID

        Returns:
            List of latest build job dicts ordered by step number.
        """
        latest_build_ids = (
            select(Job.step_number, func.max(Job.id).label("max_id"))
            .where(Job.session_id == session_id, Job.job_type == "build")
            .group_by(Job.step_number)
            .subquery()
        )

        jobs = (
            self._session.execute(
                select(Job)
                .join(latest_build_ids, Job.id == latest_build_ids.c.max_id)
                .order_by(Job.step_number.asc())
            )
            .scalars()
            .all()
        )
        return [self._job_to_dict(j) for j in jobs]

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """
        Search jobs by command/script substring using FTS.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of matching job dicts.
        """
        # FTS5 requires raw SQL - virtual tables aren't supported by ORM
        result = self._session.execute(
            text("""
                SELECT j.* FROM jobs j
                JOIN jobs_fts fts ON j.id = fts.rowid
                WHERE jobs_fts MATCH :query
                ORDER BY j.timestamp DESC
                LIMIT :limit
            """),
            {"query": query, "limit": limit},
        )
        return [dict(row._mapping) for row in result]

    def get_by_script(self, script: str, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get jobs that ran a specific script.

        Args:
            script: Script name to search for
            limit: Maximum number of results

        Returns:
            List of matching job dicts.
        """
        jobs = (
            self._session.execute(
                select(Job)
                .where(
                    (Job.script == script)
                    | (Job.script.like(f"%{script}"))
                    | (Job.command.like(f"%{script}%"))
                )
                .order_by(Job.timestamp.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [self._job_to_dict(j) for j in jobs]

    def get_all_written_files(self) -> list[dict[str, Any]]:
        """
        Get all unique written files (outputs) from all jobs.

        Returns:
            List of dicts with path, artifact_id, size, and hashes.
        """
        query = (
            select(JobOutput.path, JobOutput.artifact_id, Artifact.size)
            .join(Artifact, JobOutput.artifact_id == Artifact.id)
            .distinct()
            .order_by(JobOutput.path)
        )
        rows = self._session.execute(query).all()

        results = []
        for path, artifact_id, size in rows:
            results.append(
                {
                    "path": path,
                    "artifact_id": artifact_id,
                    "size": size,
                    "hashes": self._artifact_repository.get_hashes(artifact_id),
                }
            )
        return results

    def delete_job(self, job_id: int) -> None:
        """
        Delete a job and its input/output junction records.

        Args:
            job_id: Job database ID
        """
        self._session.execute(delete(JobInput).where(JobInput.job_id == job_id))
        self._session.execute(delete(JobOutput).where(JobOutput.job_id == job_id))
        self._session.execute(delete(Job).where(Job.id == job_id))
        self._session.flush()

    def cleanup_orphaned_artifacts(self, artifact_ids: list[str], artifact_repo) -> None:
        """
        Delete artifacts that are no longer referenced by any job or collection.

        Unlike clear_output_records, this does NOT delete JobOutput rows for
        other jobs — it only removes truly orphaned artifact records.

        Args:
            artifact_ids: List of artifact IDs to check
            artifact_repo: Artifact repository for cleanup
        """
        if not artifact_ids:
            return

        for artifact_id in artifact_ids:
            input_ref = self._session.execute(
                select(JobInput).where(JobInput.artifact_id == artifact_id).limit(1)
            ).scalar_one_or_none()
            if input_ref:
                continue

            output_ref = self._session.execute(
                select(JobOutput).where(JobOutput.artifact_id == artifact_id).limit(1)
            ).scalar_one_or_none()
            if output_ref:
                continue

            try:
                collection_ref = self._session.execute(
                    select(CollectionMember)
                    .where(CollectionMember.artifact_id == artifact_id)
                    .limit(1)
                ).scalar_one_or_none()
                if collection_ref:
                    continue
            except Exception:
                pass

            artifact_repo.delete_hashes(artifact_id)
            artifact_repo.delete(artifact_id)

        self._session.flush()

    def clear_output_records(self, artifact_ids: list[str], artifact_repo) -> None:
        """
        Remove output records and orphaned artifacts from the database.

        Args:
            artifact_ids: List of artifact IDs to potentially remove
            artifact_repo: Artifact repository for cleanup
        """
        if not artifact_ids:
            return

        # Delete output records
        self._session.execute(delete(JobOutput).where(JobOutput.artifact_id.in_(artifact_ids)))

        for artifact_id in artifact_ids:
            # Check if artifact is still referenced by inputs
            input_ref = self._session.execute(
                select(JobInput).where(JobInput.artifact_id == artifact_id).limit(1)
            ).scalar_one_or_none()
            if input_ref:
                continue

            # Check if artifact is still referenced by other outputs
            output_ref = self._session.execute(
                select(JobOutput).where(JobOutput.artifact_id == artifact_id).limit(1)
            ).scalar_one_or_none()
            if output_ref:
                continue

            # Check if artifact is in any collection
            try:
                collection_ref = self._session.execute(
                    select(CollectionMember)
                    .where(CollectionMember.artifact_id == artifact_id)
                    .limit(1)
                ).scalar_one_or_none()
                if collection_ref:
                    continue
            except Exception as e:
                self._logger.debug(
                    "Failed to check collection membership for artifact %s: %s",
                    artifact_id,
                    e,
                )

            # Artifact is orphaned, delete it
            artifact_repo.delete_hashes(artifact_id)
            artifact_repo.delete(artifact_id)

        self._session.flush()

    def link_artifact(self, job_id: int, artifact_id: str, path: str) -> bool:
        """
        Manually link an artifact as an output of a job.

        Args:
            job_id: Job database ID
            artifact_id: Artifact UUID
            path: File path

        Returns:
            True if successful, False otherwise.
        """
        try:
            job_output = JobOutput(job_id=job_id, artifact_id=artifact_id, path=path)
            self._session.add(job_output)
            self._session.flush()
            return True
        except Exception as e:
            self._logger.debug(
                "Failed to link artifact %s to job %d at path %s: %s",
                artifact_id,
                job_id,
                path,
                e,
            )
            return False

    def _job_to_dict(self, job: Job) -> dict[str, Any]:
        """Convert Job model to dict."""
        return {
            "id": job.id,
            "job_uid": job.job_uid,
            "timestamp": job.timestamp,
            "command": job.command,
            "script": job.script,
            "step_identity": job.step_identity,
            "session_id": job.session_id,
            "step_number": job.step_number,
            "step_name": job.step_name,
            "git_repo": job.git_repo,
            "git_commit": job.git_commit,
            "git_branch": job.git_branch,
            "duration_seconds": job.duration_seconds,
            "exit_code": job.exit_code,
            "synced_at": job.synced_at,
            "status": job.status,
            "job_type": job.job_type,
            "metadata": job.metadata_,
            "telemetry": job.telemetry,
        }


# Backward compatibility alias
SQLiteJobRepository = SQLAlchemyJobRepository
