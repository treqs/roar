"""
SQLAlchemy artifact repository implementation.

Handles artifact storage and retrieval operations.
"""

import json
import secrets
import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...core.interfaces.repositories import ArtifactRepository
from ..models import Artifact, ArtifactHash, Job, JobInput, JobOutput


class SQLAlchemyArtifactRepository(ArtifactRepository):
    """
    SQLAlchemy implementation of artifact repository.

    Manages content-addressed artifacts with multiple hash algorithms.
    """

    def __init__(self, session: Session):
        """
        Initialize repository with database session.

        Args:
            session: SQLAlchemy session
        """
        self._session = session

    def register(
        self,
        hashes: dict[str, str],
        size: int,
        path: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> tuple:
        """
        Register an artifact with one or more hash digests.

        Args:
            hashes: Dict of {algorithm: digest}
            size: File size in bytes
            path: Original file path (for first_seen_path)
            source_type: Source type ('https', or None for local)
            source_url: Original download URL
            metadata: JSON metadata string

        Returns:
            (artifact_id, created) - artifact_id is the UUID, created is True if new
        """
        if not hashes:
            raise ValueError("At least one hash is required")

        # Check if any hash already exists
        for algo, digest in hashes.items():
            existing_hash = self._session.execute(
                select(ArtifactHash).where(
                    ArtifactHash.algorithm == algo, ArtifactHash.digest == digest.lower()
                )
            ).scalar_one_or_none()

            if existing_hash:
                # Artifact exists - add any missing hashes
                artifact_id = existing_hash.artifact_id
                for algo2, digest2 in hashes.items():
                    hash_exists = self._session.execute(
                        select(ArtifactHash).where(
                            ArtifactHash.algorithm == algo2, ArtifactHash.digest == digest2.lower()
                        )
                    ).scalar_one_or_none()
                    if not hash_exists:
                        new_hash = ArtifactHash(
                            artifact_id=artifact_id, algorithm=algo2, digest=digest2.lower()
                        )
                        self._session.add(new_hash)
                self._session.flush()
                return artifact_id, False

        # Create new artifact
        artifact_id = secrets.token_hex(16)  # 32-char hex string like UUID
        artifact = Artifact(
            id=artifact_id,
            size=size,
            first_seen_at=time.time(),
            first_seen_path=path,
            source_type=source_type,
            source_url=source_url,
            metadata_=metadata,
        )
        self._session.add(artifact)

        # Add all hashes
        for algo, digest in hashes.items():
            artifact_hash = ArtifactHash(
                artifact_id=artifact_id, algorithm=algo, digest=digest.lower()
            )
            self._session.add(artifact_hash)

        self._session.flush()
        return artifact_id, True

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        """
        Get artifact by ID.

        Args:
            artifact_id: Artifact UUID

        Returns:
            Artifact dict with hashes, or None if not found.
        """
        artifact = self._session.get(Artifact, artifact_id)
        if not artifact:
            return None
        result = self._artifact_to_dict(artifact)
        result["hashes"] = self.get_hashes(artifact_id)
        return result

    def get_hashes(self, artifact_id: str) -> list[dict[str, Any]]:
        """
        Get all hashes for an artifact.

        Args:
            artifact_id: Artifact UUID

        Returns:
            List of hash dicts with algorithm and digest.
        """
        hashes = (
            self._session.execute(
                select(ArtifactHash).where(ArtifactHash.artifact_id == artifact_id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "algorithm": h.algorithm,
                "digest": h.digest,
            }
            for h in hashes
        ]

    def get_by_hash(self, digest: str, algorithm: str | None = None) -> dict[str, Any] | None:
        """
        Get artifact by hash digest.

        Args:
            digest: Full hash digest or prefix (minimum 8 chars)
            algorithm: Hash algorithm to search (None searches all)

        Returns:
            Artifact dict with hashes, or None if not found or ambiguous.
        """
        digest = digest.lower()

        if algorithm:
            query = (
                select(Artifact)
                .join(ArtifactHash)
                .where(ArtifactHash.algorithm == algorithm, ArtifactHash.digest.like(digest + "%"))
                .limit(2)
            )
        else:
            query = (
                select(Artifact)
                .join(ArtifactHash)
                .where(ArtifactHash.digest.like(digest + "%"))
                .limit(2)
            )

        rows = self._session.execute(query).scalars().all()
        if len(rows) == 0:
            return None
        if len(rows) > 1:
            # Ambiguous prefix
            return None

        artifact = rows[0]
        result = self._artifact_to_dict(artifact)
        hashes = self.get_hashes(artifact.id)
        result["hashes"] = hashes
        # Backward compatibility: hash is the primary hash digest
        result["hash"] = hashes[0]["digest"] if hashes else None
        return result

    def get_by_prefix(self, hash_prefix: str) -> dict[str, Any] | None:
        """
        Get artifact by hash prefix.

        Alias for get_by_hash() for semantic clarity when using prefixes.

        Args:
            hash_prefix: Hash prefix (minimum 8 chars)

        Returns:
            Artifact dict with hashes, or None if not found or ambiguous.
        """
        return self.get_by_hash(hash_prefix)

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        """
        Get the most recent artifact associated with a file path.

        Searches JobOutput first (most recent), then JobInput, then first_seen_path.

        Args:
            path: File path to search for

        Returns:
            Artifact dict with hashes, or None if not found.
        """
        # Search JobOutput for path (most recent first)
        output_query = (
            select(JobOutput.artifact_id, Job.timestamp)
            .join(Job, JobOutput.job_id == Job.id)
            .where(JobOutput.path == path)
            .order_by(Job.timestamp.desc())
            .limit(1)
        )
        result = self._session.execute(output_query).first()
        if result:
            return self.get(result[0])

        # Search JobInput
        input_query = (
            select(JobInput.artifact_id, Job.timestamp)
            .join(Job, JobInput.job_id == Job.id)
            .where(JobInput.path == path)
            .order_by(Job.timestamp.desc())
            .limit(1)
        )
        result = self._session.execute(input_query).first()
        if result:
            return self.get(result[0])

        # Check first_seen_path
        artifact = self._session.execute(
            select(Artifact).where(Artifact.first_seen_path == path).limit(1)
        ).scalar_one_or_none()
        if artifact:
            result_dict = self._artifact_to_dict(artifact)
            result_dict["hashes"] = self.get_hashes(artifact.id)
            return result_dict

        return None

    def update_upload(self, artifact_id: str, uploaded_to: str) -> None:
        """
        Record that an artifact was uploaded to a destination.

        Args:
            artifact_id: Artifact UUID
            uploaded_to: Upload destination URL
        """
        artifact = self._session.get(Artifact, artifact_id)
        if not artifact:
            return

        current = json.loads(artifact.uploaded_to) if artifact.uploaded_to else []
        if uploaded_to not in current:
            current.append(uploaded_to)
            artifact.uploaded_to = json.dumps(current)
            self._session.flush()

    def get_locations(self, artifact_id: str) -> list[dict[str, str]]:
        """
        Get all known paths where this artifact has been seen.

        Args:
            artifact_id: Artifact UUID

        Returns:
            List of dicts with 'path' key.
        """
        paths: set[str] = set()

        # From outputs
        outputs = (
            self._session.execute(
                select(JobOutput.path).where(JobOutput.artifact_id == artifact_id).distinct()
            )
            .scalars()
            .all()
        )
        paths.update(outputs)

        # From inputs
        inputs = (
            self._session.execute(
                select(JobInput.path).where(JobInput.artifact_id == artifact_id).distinct()
            )
            .scalars()
            .all()
        )
        paths.update(inputs)

        # Also include first_seen_path
        artifact = self._session.get(Artifact, artifact_id)
        if artifact and artifact.first_seen_path:
            paths.add(artifact.first_seen_path)

        return [{"path": p} for p in sorted(paths)]

    def get_all(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        Get all artifacts, most recent first.

        Args:
            limit: Maximum number of artifacts to return

        Returns:
            List of artifact dicts with hashes.
        """
        query = (
            select(Artifact, JobOutput.path)
            .outerjoin(JobOutput)
            .group_by(Artifact.id)
            .order_by(Artifact.first_seen_at.desc())
            .limit(limit)
        )
        rows = self._session.execute(query).all()

        results = []
        for artifact, path in rows:
            result = self._artifact_to_dict(artifact)
            result["path"] = path
            result["hashes"] = self.get_hashes(artifact.id)
            results.append(result)
        return results

    def get_recent_outputs(
        self, limit: int = 50, job_type: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get recently produced output artifacts with their paths.

        Args:
            limit: Maximum number of results
            job_type: Filter by job type ('run', 'build', or None for all)

        Returns:
            List of artifact dicts with hashes and job_timestamp.
        """
        query = (
            select(Artifact, JobOutput.path, Job.timestamp.label("job_timestamp"))
            .join(JobOutput, Artifact.id == JobOutput.artifact_id)
            .join(Job, JobOutput.job_id == Job.id)
        )

        if job_type == "run":
            query = query.where(Job.job_type.is_(None) | (Job.job_type == "run"))
        elif job_type == "build":
            query = query.where(Job.job_type == "build")

        query = query.order_by(Job.timestamp.desc()).limit(limit)
        rows = self._session.execute(query).all()

        results = []
        for artifact, path, job_timestamp in rows:
            result = self._artifact_to_dict(artifact)
            result["path"] = path
            result["job_timestamp"] = job_timestamp
            result["hashes"] = self.get_hashes(artifact.id)
            results.append(result)
        return results

    def count_build_outputs(self) -> int:
        """
        Count the number of output artifacts from build jobs.

        Returns:
            Number of unique build output artifacts.
        """
        count = self._session.execute(
            select(func.count(func.distinct(JobOutput.artifact_id)))
            .join(Job, JobOutput.job_id == Job.id)
            .where(Job.job_type == "build")
        ).scalar()
        return count or 0

    def get_all_outputs_with_paths(self) -> list[dict[str, Any]]:
        """
        Get all output artifacts with their paths for verification.

        Returns:
            List of dicts with artifact_id, path, size, and hashes.
        """
        query = (
            select(JobOutput.artifact_id, JobOutput.path, Artifact.size)
            .join(Artifact, JobOutput.artifact_id == Artifact.id)
            .distinct()
            .order_by(Artifact.first_seen_at.desc())
        )
        rows = self._session.execute(query).all()

        results = []
        for artifact_id, path, size in rows:
            hashes = self.get_hashes(artifact_id)
            results.append(
                {
                    "artifact_id": artifact_id,
                    "path": path,
                    "size": size,
                    "hashes": hashes,
                    # Backward compatibility: hash is the primary hash digest
                    "hash": hashes[0]["digest"] if hashes else None,
                }
            )
        return results

    def get_jobs(self, artifact_id: str) -> dict[str, list[dict[str, Any]]]:
        """
        Get jobs that produced or consumed an artifact.

        Args:
            artifact_id: Artifact UUID

        Returns:
            Dict with 'produced_by' and 'consumed_by' lists of job dicts.
        """
        produced_by = (
            self._session.execute(
                select(Job)
                .join(JobOutput, Job.id == JobOutput.job_id)
                .where(JobOutput.artifact_id == artifact_id)
                .order_by(Job.timestamp.desc())
            )
            .scalars()
            .all()
        )

        consumed_by = (
            self._session.execute(
                select(Job)
                .join(JobInput, Job.id == JobInput.job_id)
                .where(JobInput.artifact_id == artifact_id)
                .order_by(Job.timestamp.desc())
            )
            .scalars()
            .all()
        )

        return {
            "produced_by": [self._job_to_dict(j) for j in produced_by],
            "consumed_by": [self._job_to_dict(j) for j in consumed_by],
        }

    def delete_hashes(self, artifact_id: str) -> None:
        """
        Delete all hashes for an artifact.

        Args:
            artifact_id: Artifact UUID
        """
        self._session.execute(delete(ArtifactHash).where(ArtifactHash.artifact_id == artifact_id))
        self._session.flush()

    def delete(self, artifact_id: str) -> None:
        """
        Delete an artifact.

        Args:
            artifact_id: Artifact UUID
        """
        self._session.execute(delete(Artifact).where(Artifact.id == artifact_id))
        self._session.flush()

    def _artifact_to_dict(self, artifact: Artifact) -> dict[str, Any]:
        """Convert Artifact model to dict."""
        return {
            "id": artifact.id,
            "size": artifact.size,
            "first_seen_at": artifact.first_seen_at,
            "first_seen_path": artifact.first_seen_path,
            "source_type": artifact.source_type,
            "source_url": artifact.source_url,
            "uploaded_to": artifact.uploaded_to,
            "synced_at": artifact.synced_at,
            "metadata": artifact.metadata_,
        }

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
SQLiteArtifactRepository = SQLAlchemyArtifactRepository
