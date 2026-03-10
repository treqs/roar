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
import re
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

_STEP_REFERENCE_RE = re.compile(r"^@(?:B)?\d+$", re.IGNORECASE)
_SESSION_HASH_RE = re.compile(r"^[a-f0-9]{8,64}$")


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

    def register_lineage_target(
        self,
        target: str,
        roar_dir: Path,
        cwd: Path,
        dry_run: bool = False,
        as_blake3: bool = False,
        skip_confirmation: bool = False,
        confirm_callback: Callable[[list[str]], bool] | None = None,
    ) -> RegisterResult:
        """Register lineage for an artifact path, step reference, or session hash."""
        normalized_target = target.strip()
        if self._is_step_reference(normalized_target):
            return self.register_step_lineage(
                step_reference=normalized_target,
                roar_dir=roar_dir,
                cwd=cwd,
                dry_run=dry_run,
                as_blake3=as_blake3,
                skip_confirmation=skip_confirmation,
                confirm_callback=confirm_callback,
            )

        resolved_path = self._resolve_path(normalized_target, cwd)
        if resolved_path and (self._is_s3_url(normalized_target) or os.path.exists(resolved_path)):
            return self.register_artifact_lineage(
                artifact_path=normalized_target,
                roar_dir=roar_dir,
                cwd=cwd,
                dry_run=dry_run,
                as_blake3=as_blake3,
                skip_confirmation=skip_confirmation,
                confirm_callback=confirm_callback,
            )

        if self._looks_like_session_hash(normalized_target):
            return self.register_session_lineage(
                session_hash=normalized_target,
                roar_dir=roar_dir,
                cwd=cwd,
                dry_run=dry_run,
                as_blake3=as_blake3,
                skip_confirmation=skip_confirmation,
                confirm_callback=confirm_callback,
            )

        return self.register_artifact_lineage(
            artifact_path=normalized_target,
            roar_dir=roar_dir,
            cwd=cwd,
            dry_run=dry_run,
            as_blake3=as_blake3,
            skip_confirmation=skip_confirmation,
            confirm_callback=confirm_callback,
        )

    def register_step_lineage(
        self,
        step_reference: str,
        roar_dir: Path,
        cwd: Path,
        dry_run: bool = False,
        as_blake3: bool = False,
        skip_confirmation: bool = False,
        confirm_callback: Callable[[list[str]], bool] | None = None,
    ) -> RegisterResult:
        """Register lineage for a local DAG step reference like ``@4``."""
        parsed = self._parse_step_reference(step_reference)
        if parsed is None:
            return RegisterResult(success=False, error=f"Invalid DAG reference: {step_reference}")
        step_number, is_build = parsed

        with create_database_context(roar_dir) as db_ctx:
            session = db_ctx.sessions.get_active()
            if not session:
                return RegisterResult(
                    success=False,
                    error="No active session. Run 'roar run' to create a session first.",
                )
            lineage = self.lineage_collector.collect_step(
                session_id=int(session["id"]),
                step_number=step_number,
                roar_dir=roar_dir,
                job_type="build" if is_build else None,
            )

        if not lineage.jobs:
            return RegisterResult(
                success=False,
                error=f"No tracked jobs found for DAG reference {step_reference}.",
            )

        representative_hash = self._select_representative_hash(lineage)
        return self._register_collected_lineage(
            lineage=lineage,
            roar_dir=roar_dir,
            cwd=cwd,
            session_id=int(lineage.pipeline["id"]) if lineage.pipeline else None,
            artifact_hash=representative_hash,
            dry_run=dry_run,
            as_blake3=as_blake3,
            skip_confirmation=skip_confirmation,
            confirm_callback=confirm_callback,
        )

    def register_session_lineage(
        self,
        session_hash: str,
        roar_dir: Path,
        cwd: Path,
        dry_run: bool = False,
        as_blake3: bool = False,
        skip_confirmation: bool = False,
        confirm_callback: Callable[[list[str]], bool] | None = None,
    ) -> RegisterResult:
        """Register the complete local session identified by a GLaaS session hash or prefix."""
        with create_database_context(roar_dir) as db_ctx:
            session, resolved_hash, error = self._resolve_session_target(
                db_ctx=db_ctx,
                roar_dir=roar_dir,
                session_hash=session_hash,
            )
            if session is None:
                return RegisterResult(success=False, error=error or "Session not found.")
            lineage = self.lineage_collector.collect_session(int(session["id"]), roar_dir)

        return self._register_collected_lineage(
            lineage=lineage,
            roar_dir=roar_dir,
            cwd=cwd,
            session_id=int(session["id"]),
            artifact_hash="",
            dry_run=dry_run,
            as_blake3=as_blake3,
            skip_confirmation=skip_confirmation,
            confirm_callback=confirm_callback,
            session_hash_override=resolved_hash,
        )

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
            lineage = self.lineage_collector.collect([artifact_hash], roar_dir)

        return self._register_collected_lineage(
            lineage=lineage,
            roar_dir=roar_dir,
            cwd=cwd,
            session_id=int(session["id"]),
            artifact_hash=artifact_hash,
            dry_run=dry_run,
            as_blake3=as_blake3,
            skip_confirmation=skip_confirmation,
            confirm_callback=confirm_callback,
        )

    def _register_collected_lineage(
        self,
        *,
        lineage: LineageData,
        roar_dir: Path,
        cwd: Path,
        session_id: int | None,
        artifact_hash: str,
        dry_run: bool,
        as_blake3: bool,
        skip_confirmation: bool,
        confirm_callback: Callable[[list[str]], bool] | None,
        session_hash_override: str | None = None,
    ) -> RegisterResult:
        self._logger.debug(
            "Collected lineage: %d jobs, %d artifacts",
            len(lineage.jobs),
            len(lineage.artifacts),
        )

        # Step 5: Get git context
        git_context = self._get_git_context(cwd)
        if not git_context.repo or not git_context.commit:
            self._logger.warning(
                "Missing git context: repo=%s, commit=%s", git_context.repo, git_context.commit
            )

        # Step 5.5: Check for uncommitted changes (required for tagging)
        tagging_enabled = config_get("registration.tagging.enabled")
        if tagging_enabled is None:
            tagging_enabled = True
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

        session_hash = session_hash_override or self.session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session_id,
        )
        self._logger.debug("Session hash: %s", session_hash[:12])

        detected_secrets: list[str] = []
        if self.omit_filter:
            detected_secrets = self._detect_secrets_in_lineage(lineage, git_context)
            self._logger.debug("Detected %d potential secret types", len(detected_secrets))

            if detected_secrets and not skip_confirmation:
                if confirm_callback is None:
                    return RegisterResult(
                        success=False,
                        session_hash=session_hash,
                        error="Secrets detected in data. Use --yes to proceed with redacted data.",
                        secrets_detected=detected_secrets,
                        aborted_by_user=True,
                    )

                if not confirm_callback(detected_secrets):
                    return RegisterResult(
                        success=False,
                        session_hash=session_hash,
                        error="Registration aborted by user.",
                        secrets_detected=detected_secrets,
                        aborted_by_user=True,
                    )

            if detected_secrets or self.omit_filter.enabled:
                lineage = self._filter_lineage_secrets(lineage, git_context)

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

        if as_blake3:
            self.upgrade_s3_etags_to_blake3(roar_dir=roar_dir, lineage=lineage)

        if not self.glaas_client.is_configured():
            return RegisterResult(
                success=False,
                error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
            )

        try:
            self.glaas_client.health_check()
        except Exception as e:
            return RegisterResult(
                success=False,
                error=f"GLaaS health check failed: {e}",
            )

        session_result = self.session_service.register(session_hash, git_context)
        if not session_result.success:
            return RegisterResult(
                success=False,
                session_hash=session_hash,
                error=f"Session registration failed: {session_result.error}",
            )

        batch_result: BatchRegistrationResult = self.coordinator.register_lineage(
            session_hash=session_hash,
            git_context=git_context,
            jobs=self._order_jobs_for_registration(
                self._normalize_jobs_for_registration(lineage.jobs)
            ),
            artifacts=self._prepare_artifacts(lineage.artifacts, session_hash),
        )

        if tagging_enabled and git_context.commit:
            tag_name = f"roar/{git_context.commit[:8]}"
            vcs = GitVCSProvider()
            repo_root = vcs.get_repo_root(str(cwd))
            if repo_root:
                success, tag_error = vcs.create_tag(repo_root, tag_name)
                if not success:
                    self._logger.debug("Failed to create git tag: %s", tag_error)

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

    def _resolve_session_target(
        self,
        *,
        db_ctx,
        roar_dir: Path,
        session_hash: str,
    ) -> tuple[dict | None, str | None, str | None]:
        candidates: list[tuple[dict, str]] = []
        for session in db_ctx.sessions.get_all():
            resolved_hash = self.session_service.compute_session_hash(
                roar_dir=str(roar_dir),
                session_id=int(session["id"]),
            )
            if resolved_hash.startswith(session_hash):
                candidates.append((session, resolved_hash))

        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1], None
        if len(candidates) > 1:
            return None, None, (
                f"Ambiguous session hash prefix '{session_hash}'. "
                "Provide more characters to select a single local session."
            )

        local_session = db_ctx.sessions.get_by_hash_prefix(session_hash)
        if local_session:
            resolved_hash = self.session_service.compute_session_hash(
                roar_dir=str(roar_dir),
                session_id=int(local_session["id"]),
            )
            return local_session, resolved_hash, None

        return None, None, f"No local session matches '{session_hash}'."

    def _select_representative_hash(self, lineage: LineageData) -> str:
        hashes = sorted(str(hash_value) for hash_value in lineage.artifact_hashes if hash_value)
        if len(hashes) == 1:
            return hashes[0]
        return ""

    def _normalize_jobs_for_registration(self, jobs: list[dict]) -> list[dict]:
        normalized = [dict(job) for job in jobs]
        known_job_uids = {
            str(job["job_uid"]) for job in normalized if isinstance(job.get("job_uid"), str)
        }
        root_candidates = [job for job in normalized if self._is_local_parent_candidate(job)]
        if not root_candidates:
            root_candidates = [
                job
                for job in normalized
                if not str(job.get("command", "")).startswith("ray_task:")
            ]

        for job in normalized:
            parent_uid = str(job.get("parent_job_uid") or "").strip()
            if not parent_uid or parent_uid in known_job_uids:
                continue

            inferred_parent_uid = self._infer_local_parent_uid(job, root_candidates)
            if inferred_parent_uid:
                job["parent_job_uid"] = inferred_parent_uid
            else:
                job["parent_job_uid"] = None

        return normalized

    def _order_jobs_for_registration(self, jobs: list[dict]) -> list[dict]:
        jobs_by_uid = {
            str(job["job_uid"]): job for job in jobs if isinstance(job.get("job_uid"), str)
        }
        ordered: list[dict] = []
        seen: set[str] = set()

        def visit(job: dict) -> None:
            parent_uid = job.get("parent_job_uid")
            if isinstance(parent_uid, str) and parent_uid:
                parent = jobs_by_uid.get(parent_uid)
                if parent is not None:
                    visit(parent)

            visit_key = str(job.get("job_uid") or f"id:{job.get('id')}")
            if visit_key in seen:
                return
            seen.add(visit_key)
            ordered.append(job)

        for job in sorted(
            jobs,
            key=lambda item: (
                int(item.get("step_number") or 0),
                float(item.get("timestamp") or 0.0),
                int(item.get("id") or 0),
            ),
        ):
            visit(job)

        return ordered

    def _infer_local_parent_uid(self, job: dict, candidates: list[dict]) -> str | None:
        job_step = int(job.get("step_number") or 0)
        job_timestamp = float(job.get("timestamp") or 0.0)

        eligible = [
            candidate
            for candidate in candidates
            if (
                int(candidate.get("step_number") or 0) < job_step
                or (
                    int(candidate.get("step_number") or 0) == job_step
                    and float(candidate.get("timestamp") or 0.0) <= job_timestamp
                )
            )
        ]
        if not eligible:
            return None

        preferred = max(eligible, key=self._parent_candidate_sort_key)
        inferred_uid = preferred.get("job_uid")
        return str(inferred_uid) if inferred_uid else None

    def _is_local_parent_candidate(self, job: dict) -> bool:
        command = str(job.get("command", "") or "")
        job_type = str(job.get("job_type", "") or "")
        return not command.startswith("ray_task:") and job_type != "build"

    def _parent_candidate_sort_key(self, job: dict) -> tuple[int, int, float, int]:
        command = str(job.get("command", "") or "")
        return (
            1 if "ray job submit" in command else 0,
            int(job.get("step_number") or 0),
            float(job.get("timestamp") or 0.0),
            int(job.get("id") or 0),
        )

    def _is_step_reference(self, target: str) -> bool:
        return bool(_STEP_REFERENCE_RE.match(target))

    def _looks_like_session_hash(self, target: str) -> bool:
        return bool(_SESSION_HASH_RE.match(target))

    def _parse_step_reference(self, reference: str) -> tuple[int, bool] | None:
        if not self._is_step_reference(reference):
            return None
        step_ref = reference[1:]
        is_build = step_ref.upper().startswith("B")
        if is_build:
            step_ref = step_ref[1:]
        if not step_ref.isdigit():
            return None
        return int(step_ref), is_build

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
