"""
Register service for submitting artifact lineage to GLaaS.

Orchestrates the workflow:
1. Resolve artifact path and compute hash
2. Look up artifact in local database
3. Get active session and git context
4. Collect lineage via LineageCollector
5. Compute session hash
6. Detect and filter secrets with user confirmation
7. Register with GLaaS via RegistrationCoordinator
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from ...config import config_get
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import BatchRegistrationResult, GitContext
from ...core.interfaces.upload import LineageData
from ...core.logging import get_logger
from ...db.context import create_database_context
from ...db.hashing.backend import compute_hashes_batch
from ...filters.omit import OmitFilter, OmitMatch
from ...glaas_client import GlaasClient
from ...plugins.vcs.git import GitVCSProvider
from ..transfer.common import resolve_repo_url_or_local_uri
from ..upload.lineage_collector import LineageCollector
from .coordinator import RegistrationCoordinator
from .session import SessionRegistrationService

_Blake3Constructor = Callable[[], Any]

try:
    from blake3 import blake3 as _blake3_import
except Exception:
    _blake3_constructor: _Blake3Constructor | None = None
else:
    _blake3_constructor = _blake3_import

boto3 = None


def _ensure_boto3():
    global boto3
    if boto3 is None:
        import boto3 as _boto3

        boto3 = _boto3


@dataclass
class RegisterResult:
    """Result of register_artifact_lineage operation."""

    success: bool
    session_hash: str = ""
    artifact_hash: str = ""
    jobs_registered: int = 0
    artifacts_registered: int = 0
    links_created: int = 0
    error: str | None = None
    secrets_detected: list[str] = field(default_factory=list)
    secrets_redacted: bool = False
    aborted_by_user: bool = False


class RegisterService:
    """
    Service for registering artifact lineage with GLaaS.

    Orchestrates the complete registration workflow:
    1. Resolve artifact path and compute BLAKE3 hash
    2. Look up artifact in local database
    3. Get active session and extract git context
    4. Collect lineage via LineageCollector
    5. Compute session hash
    6. Detect secrets and prompt for confirmation (if interactive)
    7. If not dry-run:
       a. Check GLaaS health
       b. Register session
       c. Register lineage via RegistrationCoordinator (with secrets filtered)

    Follows SOLID principles:
    - SRP: Only handles registration orchestration
    - OCP: Extends registration without modifying existing services
    - DIP: Constructor injection for all dependencies
    """

    def __init__(
        self,
        glaas_client: GlaasClient | None = None,
        lineage_collector: LineageCollector | None = None,
        coordinator: RegistrationCoordinator | None = None,
        session_service: SessionRegistrationService | None = None,
        omit_filter: OmitFilter | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the register service.

        Args:
            glaas_client: GLaaS client for API communication
            lineage_collector: Service for collecting lineage data
            coordinator: Registration coordinator for 4-phase pattern
            session_service: Service for session registration
            omit_filter: Filter for detecting and redacting secrets
            logger: Logger instance. If None, resolves from DI container.
        """
        self._glaas_client = glaas_client
        self._lineage_collector = lineage_collector
        self._coordinator = coordinator
        self._session_service = session_service
        self._omit_filter = omit_filter

        self._logger = logger or get_logger()

    @property
    def omit_filter(self) -> OmitFilter | None:
        """Get or create omit filter from config."""
        if self._omit_filter is None:
            omit_config = config_get("registration.omit")
            if omit_config and omit_config.get("enabled", True):
                self._omit_filter = OmitFilter(omit_config)
        return self._omit_filter

    @cached_property
    def glaas_client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        return self._glaas_client or GlaasClient()

    @cached_property
    def lineage_collector(self) -> LineageCollector:
        """Get or create lineage collector."""
        return self._lineage_collector or LineageCollector()

    @cached_property
    def coordinator(self) -> RegistrationCoordinator:
        """Get or create registration coordinator."""
        return self._coordinator or RegistrationCoordinator()

    @cached_property
    def session_service(self) -> SessionRegistrationService:
        """Get or create session service."""
        return self._session_service or SessionRegistrationService()

    def register_artifact_lineage(
        self,
        artifact_path: str,
        roar_dir: Path,
        cwd: Path,
        dry_run: bool = False,
        as_blake3: bool = False,
        skip_confirmation: bool = False,
        confirm_callback: Callable[[list[str]], bool] | None = None,
    ) -> RegisterResult:
        """
        Register artifact and its lineage with GLaaS.

        Args:
            artifact_path: Path to the artifact file
            roar_dir: Path to .roar directory
            cwd: Current working directory
            dry_run: If True, show what would be registered without calling API
            as_blake3: If True, upgrade S3 etag-only artifacts to blake3 hashes
            skip_confirmation: If True, skip confirmation prompt even if secrets detected
            confirm_callback: Callback function to prompt user for confirmation.
                              Receives list of detected secret types, returns True to proceed.
                              If None and secrets are detected (and skip_confirmation=False),
                              registration will abort.

        Returns:
            RegisterResult with success status and counts
        """
        # Step 1: Resolve artifact path
        resolved_path = self._resolve_path(artifact_path, cwd)
        if not resolved_path:
            return RegisterResult(
                success=False,
                error=f"File not found: {artifact_path}",
            )

        is_s3_artifact = self._is_s3_url(resolved_path)
        if not is_s3_artifact and not os.path.exists(resolved_path):
            return RegisterResult(
                success=False,
                error=f"File not found: {artifact_path}",
            )

        # Step 2/3: Resolve hash and tracked artifact record
        with create_database_context(roar_dir) as db_ctx:
            if is_s3_artifact:
                db_artifact = db_ctx.artifacts.get_by_path(resolved_path)
                if not db_artifact:
                    return RegisterResult(
                        success=False,
                        error=f"Artifact not tracked by roar: {artifact_path}\n"
                        "Run 'roar run' to track this artifact first.",
                    )
                artifact_hash = self._select_primary_hash(db_artifact)
            else:
                artifact_hash = self._compute_hash(resolved_path)
                if not artifact_hash:
                    return RegisterResult(
                        success=False,
                        error=f"Failed to compute hash for: {artifact_path}",
                    )
                db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash, algorithm="blake3")
                if not db_artifact:
                    return RegisterResult(
                        success=False,
                        error=f"Artifact not tracked by roar: {artifact_path}\n"
                        "Run 'roar run' to track this artifact first.",
                    )

            if not artifact_hash:
                return RegisterResult(
                    success=False,
                    error=f"Artifact has no registered hash: {artifact_path}",
                )

            self._logger.debug("Artifact hash: %s", artifact_hash[:12])

            # Step 4: Get active session
            session = db_ctx.sessions.get_active()
            if not session:
                return RegisterResult(
                    success=False,
                    error="No active session. Run 'roar run' to create a session first.",
                )

            self._logger.debug("Active session: %d", session["id"])

        # Step 5: Get git context
        git_context = self._get_git_context(cwd)
        if not git_context.repo or not git_context.commit:
            self._logger.warning(
                "Missing git context: repo=%s, commit=%s", git_context.repo, git_context.commit
            )

        # Step 5.5: Check for uncommitted changes (required for tagging)
        tagging_enabled = config_get("registration.tagging.enabled")
        if tagging_enabled is None:
            tagging_enabled = True  # Default to enabled
        if tagging_enabled and git_context.commit:
            vcs = GitVCSProvider()
            repo_root = vcs.get_repo_root(str(cwd))
            if repo_root:
                clean, _changes = vcs.get_status(repo_root)
                if not clean:
                    return RegisterResult(
                        success=False,
                        artifact_hash=artifact_hash,
                        error="Cannot register with uncommitted changes. Commit your changes first.",
                    )

        # Step 6: Collect lineage
        lineage: LineageData = self.lineage_collector.collect([artifact_hash], roar_dir)
        self._logger.debug(
            "Collected lineage: %d jobs, %d artifacts",
            len(lineage.jobs),
            len(lineage.artifacts),
        )

        # Step 7: Compute session hash
        session_hash = self.session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session["id"],
        )
        self._logger.debug("Session hash: %s", session_hash[:12])

        # Step 7.5: Detect secrets in lineage data
        detected_secrets: list[str] = []
        if self.omit_filter:
            detected_secrets = self._detect_secrets_in_lineage(lineage, git_context)
            self._logger.debug("Detected %d potential secret types", len(detected_secrets))

            if detected_secrets and not skip_confirmation:
                # Need confirmation from user
                if confirm_callback is None:
                    # No callback provided, abort
                    return RegisterResult(
                        success=False,
                        session_hash=session_hash,
                        error="Secrets detected in data. Use --yes to proceed with redacted data.",
                        secrets_detected=detected_secrets,
                        aborted_by_user=True,
                    )

                # Ask user for confirmation
                if not confirm_callback(detected_secrets):
                    return RegisterResult(
                        success=False,
                        session_hash=session_hash,
                        error="Registration aborted by user.",
                        secrets_detected=detected_secrets,
                        aborted_by_user=True,
                    )

            # Filter secrets from jobs before registration
            if detected_secrets or self.omit_filter.enabled:
                lineage = self._filter_lineage_secrets(lineage, git_context)

        # Step 8: Dry-run mode - return counts without calling API
        if dry_run:
            return RegisterResult(
                success=True,
                session_hash=session_hash,
                artifact_hash=artifact_hash,
                jobs_registered=len(lineage.jobs),
                artifacts_registered=len(lineage.artifacts),
                links_created=self._estimate_links(lineage.jobs),
                secrets_detected=detected_secrets,
                secrets_redacted=bool(detected_secrets),
            )

        # Step 8.5: Upgrade S3 etag-only artifact hashes to blake3 (optional)
        if as_blake3:
            self.upgrade_s3_etags_to_blake3(roar_dir=roar_dir, lineage=lineage)

        # Step 9: Check GLaaS configuration
        if not self.glaas_client.is_configured():
            return RegisterResult(
                success=False,
                error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
            )

        # Step 10: Health check
        try:
            self.glaas_client.health_check()
        except Exception as e:
            return RegisterResult(
                success=False,
                error=f"GLaaS health check failed: {e}",
            )

        # Step 11: Register session
        session_result = self.session_service.register(session_hash, git_context)
        if not session_result.success:
            return RegisterResult(
                success=False,
                session_hash=session_hash,
                error=f"Session registration failed: {session_result.error}",
            )

        # Step 12: Register lineage via coordinator
        batch_result: BatchRegistrationResult = self.coordinator.register_lineage(
            session_hash=session_hash,
            git_context=git_context,
            jobs=lineage.jobs,
            artifacts=self._prepare_artifacts(lineage.artifacts, session_hash),
        )

        # Step 13: Create git tag if enabled
        if tagging_enabled and git_context.commit:
            tag_name = f"roar/{git_context.commit[:8]}"
            vcs = GitVCSProvider()
            repo_root = vcs.get_repo_root(str(cwd))
            if repo_root:
                success, tag_error = vcs.create_tag(repo_root, tag_name)
                if not success:
                    self._logger.debug("Failed to create git tag: %s", tag_error)

        # Build result
        if batch_result.errors:
            self._logger.warning("Registration completed with errors: %s", batch_result.errors)

        return RegisterResult(
            success=batch_result.jobs_failed == 0 and batch_result.artifacts_failed == 0,
            session_hash=session_hash,
            artifact_hash=artifact_hash,
            jobs_registered=batch_result.jobs_created,
            artifacts_registered=batch_result.artifacts_registered,
            links_created=batch_result.links_created,
            error="; ".join(batch_result.errors) if batch_result.errors else None,
            secrets_detected=detected_secrets,
            secrets_redacted=bool(detected_secrets),
        )

    def _resolve_path(self, path: str, cwd: Path) -> str | None:
        """Resolve artifact path to absolute path."""
        if os.path.isabs(path):
            return path
        if self._is_s3_url(path):
            return path
        return str(cwd / path)

    def _is_s3_url(self, path: str) -> bool:
        parsed = urlparse(path)
        return parsed.scheme == "s3" and bool(parsed.netloc)

    def _select_primary_hash(self, artifact: dict) -> str | None:
        hashes = artifact.get("hashes")
        if isinstance(hashes, list):
            by_algorithm: dict[str, str] = {}
            for entry in hashes:
                if not isinstance(entry, dict):
                    continue
                algorithm = entry.get("algorithm")
                digest = entry.get("digest")
                if isinstance(algorithm, str) and isinstance(digest, str) and digest:
                    by_algorithm.setdefault(algorithm.strip().lower(), digest)

            for algorithm in ("blake3", "sha256", "etag"):
                digest = by_algorithm.get(algorithm)
                if digest:
                    return digest

            for digest in by_algorithm.values():
                if digest:
                    return digest

        fallback = artifact.get("hash")
        if isinstance(fallback, str) and fallback:
            return fallback
        return None

    def _compute_hash(self, path: str) -> str | None:
        """Compute BLAKE3 hash of file."""
        try:
            hashes_by_path = compute_hashes_batch([path], ["blake3"])
            return hashes_by_path.get(path, {}).get("blake3")
        except (OSError, ValueError) as e:
            self._logger.error("Failed to hash file %s: %s", path, e)
            return None

    def upgrade_s3_etags_to_blake3(self, roar_dir: Path, lineage: LineageData) -> None:
        """
        Upgrade etag-only S3 artifacts in lineage to include blake3 hashes.

        This keeps existing etag rows and adds a blake3 row via INSERT OR IGNORE.
        """
        if not lineage.artifacts:
            return

        if _blake3_constructor is None:
            self._logger.warning(
                "Skipping --as-blake3 upgrade because the blake3 package is not installed."
            )
            return

        try:
            _ensure_boto3()
        except Exception as e:
            self._logger.warning("Skipping --as-blake3 upgrade because boto3 is unavailable: %s", e)
            return

        assert boto3 is not None
        s3_client = boto3.client("s3")
        with create_database_context(roar_dir) as db_ctx:
            for artifact in lineage.artifacts:
                if not self._needs_blake3_upgrade(artifact):
                    continue

                artifact_id = artifact.get("id")
                if not isinstance(artifact_id, str) or not artifact_id:
                    continue

                s3_url = self._extract_s3_url(artifact)
                if not s3_url:
                    continue

                parsed = self._parse_s3_url(s3_url)
                if parsed is None:
                    continue
                bucket, key = parsed

                digest = self._compute_s3_blake3_digest(s3_client, bucket, key)
                if not digest:
                    continue

                db_ctx.session.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO artifact_hashes (artifact_id, algorithm, digest)
                        VALUES (:artifact_id, 'blake3', :digest)
                        """
                    ),
                    {"artifact_id": artifact_id, "digest": digest},
                )

                has_blake3_row = db_ctx.session.execute(
                    text(
                        """
                        SELECT 1
                        FROM artifact_hashes
                        WHERE artifact_id = :artifact_id
                          AND algorithm = 'blake3'
                          AND digest = :digest
                        LIMIT 1
                        """
                    ),
                    {"artifact_id": artifact_id, "digest": digest},
                ).scalar_one_or_none()

                if has_blake3_row:
                    self._attach_blake3_to_artifact(artifact, digest)
                    lineage.artifact_hashes.add(digest)

    def _needs_blake3_upgrade(self, artifact: dict) -> bool:
        hashes = artifact.get("hashes")
        if not isinstance(hashes, list):
            return False

        has_etag = False
        has_blake3 = False
        for entry in hashes:
            if not isinstance(entry, dict):
                continue
            algorithm = entry.get("algorithm")
            if not isinstance(algorithm, str):
                continue
            normalized = algorithm.strip().lower()
            if normalized == "etag":
                has_etag = True
            elif normalized == "blake3":
                has_blake3 = True

        return has_etag and not has_blake3

    def _extract_s3_url(self, artifact: dict) -> str | None:
        for key in ("source_url", "first_seen_path", "path"):
            value = artifact.get(key)
            if isinstance(value, str) and value.startswith("s3://"):
                return value
        return None

    def _parse_s3_url(self, s3_url: str) -> tuple[str, str] | None:
        parsed = urlparse(s3_url)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if parsed.scheme != "s3" or not bucket or not key:
            return None
        return bucket, key

    def _compute_s3_blake3_digest(self, s3_client, bucket: str, key: str) -> str | None:
        if _blake3_constructor is None:
            return None

        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            body = response.get("Body")
            if body is None:
                return None

            hasher = _blake3_constructor()
            try:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(bytes(chunk))
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            return hasher.hexdigest()
        except Exception as e:
            self._logger.warning("Failed to compute blake3 for s3://%s/%s: %s", bucket, key, e)
            return None

    def _attach_blake3_to_artifact(self, artifact: dict, digest: str) -> None:
        hashes = artifact.get("hashes")
        if not isinstance(hashes, list):
            hashes = []
            artifact["hashes"] = hashes

        for entry in hashes:
            if not isinstance(entry, dict):
                continue
            if entry.get("algorithm") == "blake3" and entry.get("digest") == digest:
                artifact["hash"] = digest
                return

        hashes.append({"algorithm": "blake3", "digest": digest})
        artifact["hash"] = digest

    def _get_git_context(self, cwd: Path) -> GitContext:
        """Get git context from repository."""
        try:
            vcs = GitVCSProvider()
            repo_root = vcs.get_repo_root(str(cwd))
            if not repo_root:
                return GitContext(repo=None, commit=None, branch=None)

            return GitContext(
                repo=resolve_repo_url_or_local_uri(vcs, repo_root, logger=self._logger),
                commit=vcs.get_commit_hash(repo_root),
                branch=vcs.get_branch(repo_root),
            )
        except Exception as e:
            self._logger.warning("Failed to get git context: %s", e)
            return GitContext(repo=None, commit=None, branch=None)

    def _estimate_links(self, jobs: list[dict]) -> int:
        """Estimate number of artifact links from jobs."""
        links = 0
        for job in jobs:
            links += len(job.get("_inputs", []))
            links += len(job.get("_outputs", []))
        return links

    def _prepare_artifacts(self, artifacts: list[dict], session_hash: str) -> list[dict]:
        """Prepare artifacts for registration with required fields."""
        prepared = []
        for art in artifacts:
            normalized_hashes: list[dict[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for h in art.get("hashes", []):
                if not isinstance(h, dict):
                    continue
                algorithm = h.get("algorithm")
                digest = h.get("digest")
                if not isinstance(algorithm, str) or not isinstance(digest, str):
                    continue
                algorithm_name = algorithm.strip().lower()
                digest_value = digest.strip()
                if not algorithm_name or not digest_value:
                    continue
                pair = (algorithm_name, digest_value)
                if pair in seen:
                    continue
                seen.add(pair)
                normalized_hashes.append({"algorithm": algorithm_name, "digest": digest_value})

            # Prefer blake3 first when present while preserving remaining order.
            blake3_hashes = [h for h in normalized_hashes if h["algorithm"] == "blake3"]
            other_hashes = [h for h in normalized_hashes if h["algorithm"] != "blake3"]
            ordered_hashes = blake3_hashes + other_hashes

            if not ordered_hashes:
                hash_value = art.get("hash")
                if isinstance(hash_value, str) and hash_value.strip():
                    ordered_hashes = [
                        {
                            "algorithm": "blake3",
                            "digest": hash_value.strip(),
                        }
                    ]

            if not ordered_hashes:
                continue

            prepared.append(
                {
                    "hashes": ordered_hashes,
                    "size": art.get("size", 0),
                    "source_type": art.get("source_type"),
                    "session_hash": session_hash,
                }
            )
        return prepared

    def _detect_secrets_in_lineage(
        self,
        lineage: LineageData,
        git_context: GitContext,
    ) -> list[str]:
        """
        Detect potential secrets in lineage data without filtering.

        Scans commands, git URLs, and metadata for secrets.

        Args:
            lineage: Lineage data to scan
            git_context: Git context to scan

        Returns:
            List of unique detected secret pattern IDs
        """
        if not self.omit_filter:
            return []

        all_detections: list[OmitMatch] = []

        # Check git URL
        if git_context.repo:
            all_detections.extend(self.omit_filter.detect_secrets(git_context.repo, "git_url"))

        # Check each job
        for job in lineage.jobs:
            # Check command
            command = job.get("command", "")
            if command:
                all_detections.extend(self.omit_filter.detect_secrets(command, "command"))

            # Check metadata
            metadata = job.get("metadata")
            if metadata and isinstance(metadata, str):
                all_detections.extend(self.omit_filter.detect_secrets(metadata, "metadata"))

        # Return unique pattern IDs
        return self.omit_filter.get_detection_summary(all_detections)

    def _filter_lineage_secrets(
        self,
        lineage: LineageData,
        git_context: GitContext,
    ) -> LineageData:
        """
        Filter secrets from lineage data.

        Creates a new LineageData with filtered jobs.

        Args:
            lineage: Original lineage data
            git_context: Git context (for reference, not modified)

        Returns:
            New LineageData with filtered jobs
        """
        if not self.omit_filter:
            return lineage

        filtered_jobs = []
        for job in lineage.jobs:
            filtered_job = dict(job)  # Shallow copy

            # Filter command
            command = filtered_job.get("command", "")
            if command:
                filtered_command, _ = self.omit_filter.filter_command(command)
                filtered_job["command"] = filtered_command

            # Filter metadata
            metadata = filtered_job.get("metadata")
            if metadata:
                if isinstance(metadata, str):
                    filtered_metadata, _ = self.omit_filter.filter_telemetry(metadata)
                    filtered_job["metadata"] = filtered_metadata
                elif isinstance(metadata, dict):
                    filtered_metadata_dict, _ = self.omit_filter.filter_metadata(metadata)
                    filtered_job["metadata"] = filtered_metadata_dict  # type: ignore[assignment]

            filtered_jobs.append(filtered_job)

        return LineageData(
            jobs=filtered_jobs,
            artifacts=lineage.artifacts,
            artifact_hashes=lineage.artifact_hashes,
            pipeline=lineage.pipeline,
        )
