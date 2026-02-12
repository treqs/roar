"""
Put service orchestrator.

Coordinates the full put workflow: resolve sources, upload files,
register lineage with GLaaS, create job record with metadata.

roar put ALWAYS registers lineage with GLaaS. This is not optional.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...glaas_client import GlaasClient, get_glaas_url
from ...services.registration import RegistrationCoordinator, SessionRegistrationService
from ...services.transfer import (
    DatabaseContext,
    build_operation_metadata_json,
    hash_files_blake3,
    resolve_git_context,
)
from ...services.upload.lineage_collector import LineageCollector
from .backends.base import StorageBackend
from .resolver import SourceResolver


@dataclass
class PutResult:
    """Result of a put operation."""

    success: bool
    job_id: int | None = None
    job_uid: str | None = None
    session_hash: str | None = None
    session_url: str | None = None
    uploaded_files: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    would_upload: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class PutService:
    """
    Orchestrates the put workflow.

    1. Check GLaaS is configured (required)
    2. Resolve sources to file paths
    3. Hash files and find/create artifacts
    4. Upload files to storage backend
    5. Collect lineage for uploaded artifacts
    6. Register lineage with GLaaS
    7. Create job record with inputs and metadata
    """

    def __init__(
        self,
        db_context: DatabaseContext,
        backend: StorageBackend,
        destination: str,
        repo_root: Path | None = None,
        roar_dir: Path | None = None,
        glaas_client: GlaasClient | None = None,
        lineage_collector: LineageCollector | None = None,
        registration_coordinator: RegistrationCoordinator | None = None,
        session_service: SessionRegistrationService | None = None,
    ):
        """
        Initialize put service.

        Args:
            db_context: Database context for artifact/job operations.
            backend: Storage backend for uploads.
            destination: Destination URL (e.g., s3://bucket/prefix).
            repo_root: Repository root for path resolution.
            roar_dir: Path to .roar directory (for lineage collection).
            glaas_client: GLaaS client (optional, for testing).
            lineage_collector: Lineage collector (optional, for testing).
            registration_coordinator: Registration coordinator (optional, for testing).
            session_service: Session registration service (optional, for testing).
        """
        self._db = db_context
        self._backend = backend
        self._destination = destination
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._roar_dir = Path(roar_dir) if roar_dir else self._repo_root / ".roar"
        self._resolver = SourceResolver(
            repo_root=self._repo_root,
            session_repo=db_context.sessions,
            job_repo=db_context.jobs,
        )
        self._logger = get_logger()
        # Dependency injection for testing
        self._glaas_client = glaas_client
        self._lineage_collector = lineage_collector
        self._registration_coordinator = registration_coordinator
        self._session_service = session_service

        self._logger.debug(
            "PutService initialized: destination=%s, repo_root=%s, roar_dir=%s, backend=%s",
            destination,
            self._repo_root,
            self._roar_dir,
            type(backend).__name__,
        )

    def put(
        self,
        sources: list[str],
        message: str,
        dry_run: bool = False,
        git_commit: str | None = None,
        git_tag: str | None = None,
    ) -> PutResult:
        """
        Execute a put operation.

        Args:
            sources: List of source specifications to upload.
            message: Publish message for audit trail.
            dry_run: If True, show what would be done without doing it.
            git_commit: Git commit SHA at time of upload.
            git_tag: Git tag created for this upload.

        Returns:
            PutResult with operation details.

        Raises:
            ValueError: If no active session or GLaaS not configured.
            FileNotFoundError: If source files don't exist.
        """
        self._logger.debug(
            "put() called: sources=%s, message=%r, dry_run=%s, git_commit=%s, git_tag=%s",
            sources,
            message,
            dry_run,
            git_commit,
            git_tag,
        )

        # Check GLaaS is configured (required for put)
        glaas_url = get_glaas_url()
        self._logger.debug("GLaaS URL: %s", glaas_url)
        if not glaas_url:
            raise ValueError(
                "GLaaS is not configured. Run 'roar config set glaas.url <url>' to configure."
            )

        # Get or create GLaaS client
        client = self._glaas_client or GlaasClient(glaas_url)

        # Health check GLaaS
        self._logger.debug("Running GLaaS health check against %s", glaas_url)
        try:
            client.health_check()
            self._logger.debug("GLaaS health check passed")
        except Exception as e:
            self._logger.debug("GLaaS health check failed: %s", e)
            raise ValueError(f"GLaaS health check failed: {e}") from e

        # Check for active session
        active_session = self._db.sessions.get_active()
        if active_session is None:
            raise ValueError("No active session")

        session_id = active_session["id"]
        self._logger.debug("Active session: id=%s", session_id)

        # Get git context for registration
        git_context = self._get_git_context(git_commit)
        self._logger.debug(
            "Git context: repo=%s, commit=%s, branch=%s",
            git_context.repo,
            git_context.commit,
            git_context.branch,
        )

        # Compute session hash and register session with GLaaS
        session_service = self._session_service or SessionRegistrationService(client)
        session_hash = session_service.compute_session_hash(
            roar_dir=str(self._roar_dir),
            session_id=session_id,
        )
        self._logger.debug("Session hash: %s", session_hash[:12])

        # Register session with GLaaS (must happen before job/artifact registration)
        self._logger.debug("Registering session with GLaaS")
        session_result = session_service.register(session_hash, git_context)
        if not session_result.success:
            self._logger.debug("Session registration failed: %s", session_result.error)
            raise ValueError(f"Session registration failed: {session_result.error}")
        self._logger.debug(
            "Session registered: url=%s",
            session_result.session_url,
        )

        # Resolve sources to files
        self._logger.debug("Resolving sources: %s", sources)
        resolved = self._resolver.resolve(sources)
        self._logger.debug("Resolved %d source file(s)", len(resolved))

        if dry_run:
            self._logger.debug("Dry run mode — skipping upload and registration")
            return PutResult(
                success=True,
                session_hash=session_hash,
                session_url=session_result.session_url,
                dry_run=True,
                would_upload=[{"path": str(r.path), "exists": r.exists} for r in resolved],
            )

        # Process each file: hash, create artifact, upload
        uploaded_files: list[dict[str, Any]] = []
        artifact_urls: dict[str, str] = {}  # artifact_id -> remote_url
        artifact_hashes: list[str] = []  # For lineage collection
        artifacts_info: list[tuple[str, str]] = []  # (artifact_id, path)
        hashes_by_path = self._hash_files_batch([source.path for source in resolved])

        for i, source in enumerate(resolved):
            file_path = source.path

            # Hashes are computed in one batch call up front.
            file_hash = hashes_by_path.get(str(file_path))
            if not file_hash:
                raise OSError(f"Failed to hash file: {file_path}")
            self._logger.debug(
                "File %d/%d: %s, blake3=%s",
                i + 1,
                len(resolved),
                file_path.name,
                file_hash[:12],
            )

            # Find or create artifact
            artifact_id = self._find_or_create_artifact(file_path, file_hash)
            self._logger.debug("Artifact: id=%s, path=%s", artifact_id, file_path)

            # Use relative_key from resolver (relative to source dir for dirs, filename for files)
            remote_key = source.relative_key or file_path.name
            self._logger.debug("Remote key: %s", remote_key)

            # Upload to backend
            remote_url = self._backend.upload(file_path, remote_key)
            self._logger.debug("Uploaded: %s -> %s", file_path.name, remote_url)

            uploaded_files.append(
                {
                    "local_path": str(file_path),
                    "remote_url": remote_url,
                    "artifact_id": artifact_id,
                    "hash": file_hash,
                }
            )
            artifact_urls[artifact_id] = remote_url
            artifact_hashes.append(file_hash)
            artifacts_info.append((artifact_id, str(file_path)))

        self._logger.debug(
            "Upload complete: %d file(s), collecting lineage for hashes: %s",
            len(uploaded_files),
            [h[:12] for h in artifact_hashes],
        )

        # Collect lineage for all uploaded artifacts (merged)
        collector = self._lineage_collector or LineageCollector()
        lineage = collector.collect(artifact_hashes, self._roar_dir)
        self._logger.debug(
            "Lineage collected: %d job(s), %d artifact(s)",
            len(lineage.jobs),
            len(lineage.artifacts),
        )

        # Register lineage with GLaaS (session already registered above)
        coordinator = self._registration_coordinator or RegistrationCoordinator()

        # Prepare artifacts for registration (add session_hash)
        prepared_artifacts = self._prepare_artifacts_for_registration(
            lineage.artifacts, session_hash
        )
        self._logger.debug("Prepared %d artifact(s) for registration", len(prepared_artifacts))

        self._logger.debug(
            "Registering lineage: session=%s, jobs=%d, artifacts=%d",
            session_hash[:12],
            len(lineage.jobs),
            len(prepared_artifacts),
        )
        registration_result = coordinator.register_lineage(
            session_hash=session_hash or "",
            git_context=git_context,
            jobs=lineage.jobs,
            artifacts=prepared_artifacts,
        )
        self._logger.debug(
            "Registration result: jobs=%d/%d, artifacts=%d/%d, links=%d, errors=%d",
            registration_result.jobs_created,
            registration_result.jobs_created + registration_result.jobs_failed,
            registration_result.artifacts_registered,
            registration_result.artifacts_registered + registration_result.artifacts_failed,
            registration_result.links_created,
            len(registration_result.errors),
        )

        # Check for registration errors
        registration_error = None
        if registration_result.errors:
            registration_error = "; ".join(registration_result.errors)
            self._logger.debug("Registration errors: %s", registration_error)

        # Build command string
        source_str = " ".join(sources) if sources else "(session outputs)"
        command = f'roar put {source_str} -m "{message}"'

        # Determine destination type from URL scheme
        from urllib.parse import urlparse

        parsed = urlparse(self._destination)
        destination_type = parsed.scheme.lower()

        # Build metadata
        metadata_json = build_operation_metadata_json(
            "put",
            {
                "message": message,
                "destination": self._destination,
                "destination_type": destination_type,
                "artifacts": artifact_urls,
                "git_commit": git_commit,
                "git_tag": git_tag,
                "timestamp": time.time(),
            },
        )
        self._logger.debug(
            "Job metadata: destination_type=%s, artifacts=%d", destination_type, len(artifact_urls)
        )

        # Create job record
        step_number = self._db.sessions.get_next_step_number(session_id)
        job_id, job_uid = self._db.jobs.create(
            command=command,
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            metadata=metadata_json,
            job_type="put",
            exit_code=0,
        )
        self._logger.debug(
            "Job created: id=%s, uid=%s, step=%d",
            job_id,
            job_uid,
            step_number,
        )

        # Link artifacts as inputs (local)
        for artifact_id, path in artifacts_info:
            self._db.jobs.add_input(job_id, artifact_id, path)
        self._logger.debug("Linked %d artifact(s) as job inputs", len(artifacts_info))

        # Register the put job itself with GLaaS (the sink node)
        self._logger.debug("Registering put job with GLaaS: job_uid=%s, job_type=put", job_uid)
        put_job_result = coordinator.job_service.create_job(
            command=command,
            timestamp=time.time(),
            session_hash=session_hash or "",
            job_uid=job_uid,
            git_commit=git_context.commit or "",
            git_branch=git_context.branch or "",
            duration_seconds=0.0,
            exit_code=0,
            job_type="put",
            step_number=step_number,
            metadata=metadata_json,
        )
        if not put_job_result.success:
            self._logger.debug("Put job GLaaS registration failed: %s", put_job_result.error)
            if put_job_result.error:
                registration_result.errors.append(f"Put job: {put_job_result.error}")

        # Link uploaded artifacts as inputs to the put job on GLaaS
        input_artifacts = [{"hash": f["hash"], "path": f["local_path"]} for f in uploaded_files]
        if input_artifacts:
            link_result = coordinator.job_service.link_job_artifacts(
                session_hash=session_hash or "",
                job_uid=job_uid,
                inputs=input_artifacts,
                outputs=[],
            )
            if not link_result.success:
                self._logger.debug("Put job input linking failed: %s", link_result.error)
                if link_result.error:
                    registration_result.errors.append(f"Put job links: {link_result.error}")

        # Return result (include registration error if any)
        if registration_result.jobs_failed > 0 or registration_result.artifacts_failed > 0:
            self._logger.debug(
                "Put completed with registration errors: jobs_failed=%d, artifacts_failed=%d",
                registration_result.jobs_failed,
                registration_result.artifacts_failed,
            )
            return PutResult(
                success=False,
                job_id=job_id,
                job_uid=job_uid,
                session_hash=session_hash,
                session_url=session_result.session_url,
                uploaded_files=uploaded_files,
                error=registration_error,
            )

        self._logger.debug(
            "Put succeeded: %d file(s) uploaded, job_id=%s",
            len(uploaded_files),
            job_id,
        )
        return PutResult(
            success=True,
            job_id=job_id,
            job_uid=job_uid,
            session_hash=session_hash,
            session_url=session_result.session_url,
            uploaded_files=uploaded_files,
        )

    def _prepare_artifacts_for_registration(
        self, artifacts: list[dict], session_hash: str | None
    ) -> list[dict]:
        """Prepare artifacts for registration with required fields."""
        prepared = []
        for art in artifacts:
            # Get the blake3 hash
            art_hash = art.get("hash")
            if not art_hash:
                for h in art.get("hashes", []):
                    if h.get("algorithm") == "blake3":
                        art_hash = h.get("digest")
                        break

            if not art_hash:
                continue

            prepared.append(
                {
                    "hashes": [{"algorithm": "blake3", "digest": art_hash}],
                    "size": art.get("size", 0),
                    "source_type": art.get("source_type"),
                    "session_hash": session_hash or "",
                }
            )
        return prepared

    def _hash_files_batch(self, paths: list[Path]) -> dict[str, str]:
        """Compute BLAKE3 hashes for paths in one backend batch call."""
        if not paths:
            return {}

        self._logger.debug("Hashing %d file(s) in batch", len(paths))
        result = hash_files_blake3(paths)
        self._logger.debug("Batch hash completed: %d/%d file(s)", len(result), len(paths))
        return result

    def _find_or_create_artifact(self, path: Path, file_hash: str) -> str:
        """Find existing artifact by hash or create new one."""
        # Use register which handles both finding existing and creating new
        # Use blake3 to match the tracer's algorithm
        artifact_id, created = self._db.artifacts.register(
            hashes={"blake3": file_hash},
            size=path.stat().st_size,
            path=str(path),
        )
        self._logger.debug(
            "Artifact %s: id=%s (%s)",
            file_hash[:12],
            artifact_id,
            "created" if created else "existing",
        )
        return artifact_id

    def _get_git_context(self, git_commit: str | None = None) -> GitContext:
        """Get git context from repository."""
        self._logger.debug("Resolving git context from %s", self._repo_root)
        ctx = resolve_git_context(self._repo_root, git_commit)
        self._logger.debug(
            "Git context resolved: repo=%s, commit=%s, branch=%s",
            ctx.repo,
            ctx.commit[:12] if ctx.commit else None,
            ctx.branch,
        )
        return ctx
