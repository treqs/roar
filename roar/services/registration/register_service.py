"""
Register service for submitting artifact lineage to GLaaS.

Owns the registration mechanics after local lineage has already been collected.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from ...application.publish.git import (
    build_publish_tag_name,
    create_publish_git_tag,
    ensure_clean_publish_repo,
    resolve_publish_git_context,
)
from ...application.publish.registration import (
    CompositeRegistrationCandidate,
    ensure_composite_hash_entry,
    extract_composite_digest,
    normalize_registration_hashes,
    normalize_registration_source_type,
    prepare_batch_registration_artifacts,
    preregister_lineage_composites,
    register_publish_lineage,
)
from ...application.publish.session import prepare_publish_session
from ...config import config_get
from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...db.context import create_database_context, optional_repo
from ...execution.framework.registry import (
    is_execution_noise_job,
    is_execution_submit_job,
    is_execution_task_job,
)
from ...filters.omit import OmitFilter, OmitMatch
from ...glaas_client import GlaasClient
from ..put.composite_builder import CompositeArtifactBuilder, CompositeLeaf
from . import _artifact_ref
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

    Runs the shared registration mechanics after the application layer has
    already resolved the local target and collected the lineage bundle.

    If not dry-run:
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
        coordinator: RegistrationCoordinator | None = None,
        session_service: SessionRegistrationService | None = None,
        composite_builder: CompositeArtifactBuilder | None = None,
        omit_filter: OmitFilter | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the register service.

        Args:
            glaas_client: GLaaS client for API communication
            coordinator: Registration coordinator for 4-phase pattern
            session_service: Service for session registration
            omit_filter: Filter for detecting and redacting secrets
            logger: Logger instance. If None, resolves from DI container.
        """
        self._glaas_client = glaas_client
        self._coordinator = coordinator
        self._session_service = session_service
        self._composite_builder = composite_builder or CompositeArtifactBuilder()
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
    def coordinator(self) -> RegistrationCoordinator:
        """Get or create registration coordinator."""
        return self._coordinator or RegistrationCoordinator()

    @cached_property
    def session_service(self) -> SessionRegistrationService:
        """Get or create session service."""
        return self._session_service or SessionRegistrationService()

    def register_collected_lineage(
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
        """Register already-collected local lineage with GLaaS."""
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
            try:
                ensure_clean_publish_repo(
                    cwd,
                    error_message="Cannot register with uncommitted changes. Commit your changes first.",
                )
            except ValueError as exc:
                return RegisterResult(
                    success=False,
                    artifact_hash=artifact_hash,
                    error=str(exc),
                )

        publish_session = prepare_publish_session(
            glaas_client=self.glaas_client,
            session_service=self.session_service,
            roar_dir=roar_dir,
            session_id=session_id,
            git_context=git_context,
            logger=self._logger,
            register_with_glaas=False,
            session_hash_override=session_hash_override,
        )
        session_hash = publish_session.session_hash

        omit_filter = self.omit_filter
        detected_secrets: list[str] = []
        if omit_filter:
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

        if detected_secrets or (omit_filter is not None and omit_filter.enabled):
            lineage = self._filter_lineage_secrets(lineage, git_context)

        registration_jobs = self._order_jobs_for_registration(
            self._normalize_jobs_for_registration(lineage.jobs)
        )

        if dry_run:
            return RegisterResult(
                success=True,
                session_hash=session_hash,
                artifact_hash=artifact_hash,
                jobs_registered=len(registration_jobs),
                artifacts_registered=len(lineage.artifacts),
                links_created=self._estimate_links(registration_jobs),
                secrets_detected=detected_secrets,
                secrets_redacted=bool(detected_secrets),
            )

        if as_blake3:
            self.upgrade_s3_etags_to_blake3(roar_dir=roar_dir, lineage=lineage)

        try:
            prepare_publish_session(
                glaas_client=self.glaas_client,
                session_service=self.session_service,
                roar_dir=roar_dir,
                session_id=session_id,
                git_context=git_context,
                logger=self._logger,
                register_with_glaas=True,
                configured_error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
                session_hash_override=session_hash,
            )
        except ValueError as exc:
            return RegisterResult(
                success=False,
                session_hash=session_hash,
                error=str(exc),
            )

        composite_registrations: list[dict[str, Any]] = []
        pre_registration_errors: list[str] = []
        if self._has_lineage_composites(lineage.artifacts):
            try:
                with create_database_context(roar_dir) as db_ctx:
                    composite_registrations = self._register_lineage_composites_with_glaas(
                        db_ctx=db_ctx,
                        lineage_artifacts=lineage.artifacts,
                        session_hash=session_hash,
                        registration_errors=pre_registration_errors,
                    )
            except Exception as e:
                return RegisterResult(
                    success=False,
                    session_hash=session_hash,
                    artifact_hash=artifact_hash,
                    error=f"Composite artifact registration failed: {e}",
                    secrets_detected=detected_secrets,
                    secrets_redacted=bool(detected_secrets),
                )

        self._refresh_job_artifact_references(lineage.jobs, lineage.artifacts)

        if session_id is not None:
            with create_database_context(roar_dir) as db_ctx:
                batch_result = register_publish_lineage(
                    coordinator=self.coordinator,
                    glaas_client=self.glaas_client,
                    session_hash=session_hash,
                    git_context=git_context,
                    jobs=registration_jobs,
                    artifacts=prepare_batch_registration_artifacts(
                        lineage.artifacts,
                        session_hash,
                        fallback_to_hash=True,
                        prefer_blake3_first=True,
                    ),
                    db_ctx=db_ctx,
                    session_id=session_id,
                    label_artifacts=lineage.artifacts,
                )
        else:
            batch_result = register_publish_lineage(
                coordinator=self.coordinator,
                glaas_client=self.glaas_client,
                session_hash=session_hash,
                git_context=git_context,
                jobs=registration_jobs,
                artifacts=prepare_batch_registration_artifacts(
                    lineage.artifacts,
                    session_hash,
                    fallback_to_hash=True,
                    prefer_blake3_first=True,
                ),
                db_ctx=None,
                session_id=None,
                label_artifacts=lineage.artifacts,
            )

        if tagging_enabled and git_context.commit:
            tag_name = build_publish_tag_name(git_context.commit, short=True)
            success, tag_error = create_publish_git_tag(cwd, tag_name)
            if not success:
                self._logger.debug("Failed to create git tag: %s", tag_error)

        composite_registered = sum(1 for item in composite_registrations if item.get("registered"))
        composite_failed = sum(1 for item in composite_registrations if not item.get("registered"))
        total_artifacts_registered = batch_result.artifacts_registered + composite_registered
        total_artifacts_failed = batch_result.artifacts_failed + composite_failed
        all_errors = pre_registration_errors + batch_result.errors

        if all_errors:
            self._logger.warning("Registration completed with errors: %s", all_errors)

        return RegisterResult(
            success=batch_result.jobs_failed == 0 and total_artifacts_failed == 0,
            session_hash=session_hash,
            artifact_hash=artifact_hash,
            jobs_registered=batch_result.jobs_created,
            artifacts_registered=total_artifacts_registered,
            links_created=batch_result.links_created,
            error="; ".join(all_errors) if all_errors else None,
            secrets_detected=detected_secrets,
            secrets_redacted=bool(detected_secrets),
        )

    def _normalize_jobs_for_registration(self, jobs: list[dict]) -> list[dict]:
        normalized = [dict(job) for job in jobs if not self._is_registration_noise_job(job)]
        known_job_uids = {
            str(job["job_uid"]) for job in normalized if isinstance(job.get("job_uid"), str)
        }
        root_candidates = [job for job in normalized if self._is_local_parent_candidate(job)]
        if not root_candidates:
            root_candidates = [
                job
                for job in normalized
                if not is_execution_task_job(job)
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

    def _is_registration_noise_job(self, job: dict) -> bool:
        return is_execution_noise_job(job)

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
        job_type = str(job.get("job_type", "") or "")
        return not is_execution_task_job(job) and not is_execution_noise_job(job) and job_type != "build"

    def _parent_candidate_sort_key(self, job: dict) -> tuple[int, int, float, int]:
        return (
            1 if is_execution_submit_job(job) else 0,
            int(job.get("step_number") or 0),
            float(job.get("timestamp") or 0.0),
            int(job.get("id") or 0),
        )

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

        with create_database_context(roar_dir) as db_ctx:
            for artifact in lineage.artifacts:
                if not self._needs_blake3_upgrade(artifact):
                    continue

                digest = self._ensure_artifact_blake3_digest(
                    db_ctx=db_ctx,
                    artifact=artifact,
                )
                if digest:
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

    def _ensure_artifact_blake3_digest(
        self,
        *,
        db_ctx: Any,
        artifact: dict[str, Any],
    ) -> str | None:
        existing_digest = self._select_hash_by_algorithm(artifact, "blake3")
        if existing_digest is not None:
            return existing_digest

        if not self._needs_blake3_upgrade(artifact):
            return None

        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return None

        s3_url = self._extract_s3_url(artifact)
        if not s3_url:
            return None

        parsed = self._parse_s3_url(s3_url)
        if parsed is None:
            return None
        bucket, key = parsed

        try:
            _ensure_boto3()
        except Exception as e:
            self._logger.warning(
                "Skipping blake3 upgrade for %s because boto3 is unavailable: %s",
                s3_url,
                e,
            )
            return None

        assert boto3 is not None
        digest = self._compute_s3_blake3_digest(boto3.client("s3"), bucket, key)
        if not digest:
            return None

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
        if not has_blake3_row:
            return None

        self._attach_blake3_to_artifact(artifact, digest)
        return digest

    @staticmethod
    def _select_hash_by_algorithm(artifact: dict[str, Any], algorithm: str) -> str | None:
        hashes = artifact.get("hashes")
        if not isinstance(hashes, list):
            return None

        target = algorithm.strip().lower()
        for entry in hashes:
            if not isinstance(entry, dict):
                continue
            current_algorithm = entry.get("algorithm")
            digest = entry.get("digest")
            if (
                isinstance(current_algorithm, str)
                and current_algorithm.strip().lower() == target
                and isinstance(digest, str)
                and digest
            ):
                return digest.lower()

        return None

    def _get_git_context(self, cwd: Path) -> GitContext:
        """Get git context from repository."""
        return resolve_publish_git_context(cwd, logger=self._logger)

    def _estimate_links(self, jobs: list[dict]) -> int:
        """Estimate number of artifact links from jobs."""
        links = 0
        for job in jobs:
            links += len(job.get("_inputs", []))
            links += len(job.get("_outputs", []))
        return links

    def _refresh_job_artifact_references(
        self,
        jobs: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> None:
        preferred_hash_by_path: dict[str, str] = {}
        preferred_hash_by_digest: dict[str, str] = {}

        for artifact in artifacts:
            preferred_hash = self._select_preferred_link_hash(artifact)
            if preferred_hash is None:
                continue

            artifact_path = _artifact_ref.artifact_path(artifact)
            if artifact_path:
                preferred_hash_by_path[artifact_path] = preferred_hash

            for entry in self._extract_registration_hashes(artifact):
                digest = entry.get("digest")
                if isinstance(digest, str) and digest:
                    preferred_hash_by_digest[digest] = preferred_hash

        for job in jobs:
            self._refresh_job_io_refs(
                job, "_inputs", "_input_hashes", preferred_hash_by_path, preferred_hash_by_digest
            )
            self._refresh_job_io_refs(
                job, "_outputs", "_output_hashes", preferred_hash_by_path, preferred_hash_by_digest
            )

    def _refresh_job_io_refs(
        self,
        job: dict[str, Any],
        items_key: str,
        hashes_key: str,
        preferred_hash_by_path: dict[str, str],
        preferred_hash_by_digest: dict[str, str],
    ) -> None:
        items = job.get(items_key)
        if not isinstance(items, list):
            return

        updated_hashes: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            preferred_hash: str | None = None
            path = item.get("path")
            if isinstance(path, str) and path:
                preferred_hash = preferred_hash_by_path.get(path)

            raw_hash = item.get("hash")
            if preferred_hash is None and isinstance(raw_hash, str) and raw_hash:
                preferred_hash = preferred_hash_by_digest.get(raw_hash.lower())

            if preferred_hash is not None:
                item["hash"] = preferred_hash

            item_hash = item.get("hash")
            if isinstance(item_hash, str) and item_hash:
                updated_hashes.append(item_hash)

        job[hashes_key] = updated_hashes

    def _has_lineage_composites(self, artifacts: list[dict[str, Any]]) -> bool:
        return any(
            extract_composite_digest(self._extract_registration_hashes(artifact))
            for artifact in artifacts
        )

    def _select_preferred_link_hash(self, artifact: dict[str, Any]) -> str | None:
        hashes = self._extract_registration_hashes(artifact)
        for preferred_algorithm in ("blake3", "composite-blake3"):
            for entry in hashes:
                if entry.get("algorithm") == preferred_algorithm:
                    digest = entry.get("digest")
                    if isinstance(digest, str) and digest:
                        return digest
        if hashes:
            digest = hashes[0].get("digest")
            if isinstance(digest, str) and digest:
                return digest
        return None

    def _register_lineage_composites_with_glaas(
        self,
        *,
        db_ctx: Any,
        lineage_artifacts: list[dict[str, Any]],
        session_hash: str,
        registration_errors: list[str],
    ) -> list[dict[str, Any]]:
        payloads = self._build_lineage_composite_payloads(
            db_ctx=db_ctx,
            lineage_artifacts=lineage_artifacts,
            session_hash=session_hash,
        )
        return preregister_lineage_composites(
            glaas_client=self.glaas_client,
            payloads=payloads,
            registration_errors=registration_errors,
            logger=self._logger,
        )

    def _build_lineage_composite_payloads(
        self,
        *,
        db_ctx: Any,
        lineage_artifacts: list[dict[str, Any]],
        session_hash: str,
    ) -> list[CompositeRegistrationCandidate]:
        composites_repo: Any = optional_repo(db_ctx, "composites")
        lineage_artifacts_by_id = {
            str(artifact_id): artifact
            for artifact in lineage_artifacts
            if isinstance((artifact_id := artifact.get("id")), str) and artifact_id
        }
        payloads: list[CompositeRegistrationCandidate] = []
        seen_hashes: set[str] = set()

        for artifact in lineage_artifacts:
            hashes = self._extract_registration_hashes(artifact)
            composite_digest = extract_composite_digest(hashes)
            if not composite_digest or composite_digest in seen_hashes:
                continue

            component_rows: list[dict[str, Any]] = []
            membership_index: dict[str, Any] | None = None
            artifact_id = artifact.get("id")
            if composites_repo is not None and isinstance(artifact_id, str) and artifact_id:
                rows = composites_repo.get_components(artifact_id, limit=5000)
                if isinstance(rows, list):
                    component_rows = [row for row in rows if isinstance(row, dict)]

                raw_membership = composites_repo.get_membership_index(artifact_id)
                if isinstance(raw_membership, dict):
                    membership_index = raw_membership

            components = self._normalize_lineage_components(
                component_rows,
                db_ctx=db_ctx,
                lineage_artifacts_by_id=lineage_artifacts_by_id,
            )
            component_count_total = self._resolve_lineage_component_count_total(
                artifact_component_count=artifact.get("component_count"),
                membership_index=membership_index,
                stored_components=len(components),
            )
            if component_count_total <= 0:
                self._logger.warning(
                    "Skipping lineage composite %s: missing component_count metadata",
                    composite_digest[:12],
                )
                continue

            membership_payload = self._build_lineage_membership_index_payload(
                membership_index=membership_index,
                component_count_total=component_count_total,
                components=components,
            )
            normalized_hashes = ensure_composite_hash_entry(hashes, composite_digest)
            root_path = _artifact_ref.artifact_path(artifact) or ""
            source_type = normalize_registration_source_type(artifact.get("source_type"))
            try:
                size = max(0, int(artifact.get("size", 0)))
            except (TypeError, ValueError):
                size = 0

            seen_hashes.add(composite_digest)
            payloads.append(
                CompositeRegistrationCandidate(
                    hash=composite_digest,
                    root_path=str(root_path),
                    component_count_total=component_count_total,
                    component_count_stored=len(components),
                    payload={
                        "hash": composite_digest,
                        "hashes": normalized_hashes,
                        "size": size,
                        "source_type": source_type,
                        "session_hash": session_hash,
                        "component_count_total": component_count_total,
                        "components": components,
                        "membership_index": membership_payload,
                    },
                )
            )

        return payloads

    def _normalize_lineage_components(
        self,
        component_rows: list[dict[str, Any]],
        *,
        db_ctx: Any,
        lineage_artifacts_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        components: list[dict[str, Any]] = []

        for row in component_rows:
            relative_path = row.get("relative_path")
            if not isinstance(relative_path, str) or not relative_path:
                continue
            resolved_component = self._resolve_component_hash_for_registration(
                row=row,
                db_ctx=db_ctx,
                lineage_artifacts_by_id=lineage_artifacts_by_id,
            )
            if resolved_component is None:
                continue
            component_algorithm, digest = resolved_component

            leaf_kind = row.get("leaf_kind")
            if not isinstance(leaf_kind, str) or not leaf_kind:
                leaf_kind = "file"

            component_size = row.get("component_size")
            try:
                if isinstance(component_size, bool):
                    size_value = int(component_size)
                elif isinstance(component_size, int | float | str):
                    size_value = max(0, int(component_size))
                else:
                    size_value = 0
            except (TypeError, ValueError):
                size_value = 0

            component_type = row.get("component_type")
            if component_type is not None and not isinstance(component_type, str):
                component_type = None

            components.append(
                {
                    "relative_path": relative_path,
                    "leaf_kind": leaf_kind,
                    "component_algorithm": component_algorithm,
                    "component_digest": digest.lower(),
                    "component_size": size_value,
                    "component_type": component_type,
                }
            )

        return components

    def _resolve_component_hash_for_registration(
        self,
        *,
        row: dict[str, Any],
        db_ctx: Any,
        lineage_artifacts_by_id: dict[str, dict[str, Any]],
    ) -> tuple[str, str] | None:
        artifact_id = row.get("artifact_id")
        linked_artifact: dict[str, Any] | None = None

        if isinstance(artifact_id, str) and artifact_id:
            linked_artifact = lineage_artifacts_by_id.get(artifact_id)
            if linked_artifact is None:
                artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
                if artifacts_repo is not None:
                    loaded_artifact = artifacts_repo.get(artifact_id)
                    if isinstance(loaded_artifact, dict):
                        linked_artifact = loaded_artifact
                        lineage_artifacts_by_id[artifact_id] = loaded_artifact

        if linked_artifact is not None:
            blake3_digest = self._select_hash_by_algorithm(linked_artifact, "blake3")
            if blake3_digest is None:
                blake3_digest = self._ensure_artifact_blake3_digest(
                    db_ctx=db_ctx,
                    artifact=linked_artifact,
                )
            if blake3_digest is not None:
                return "blake3", blake3_digest

        component_algorithm = row.get("component_algorithm")
        component_digest = row.get("component_digest")
        if (
            isinstance(component_algorithm, str)
            and component_algorithm.strip().lower() == "blake3"
            and isinstance(component_digest, str)
            and component_digest
        ):
            return "blake3", component_digest.lower()

        self._logger.warning(
            "Skipping lineage composite component %s: missing resolvable blake3 digest",
            row.get("relative_path") or "<unknown>",
        )
        return None

    @staticmethod
    def _resolve_lineage_component_count_total(
        artifact_component_count: Any,
        membership_index: dict[str, Any] | None,
        stored_components: int,
    ) -> int:
        try:
            artifact_total = int(artifact_component_count)
        except (TypeError, ValueError):
            artifact_total = 0

        membership_total = 0
        if isinstance(membership_index, dict):
            try:
                membership_total = int(membership_index.get("total_components", 0))
            except (TypeError, ValueError):
                membership_total = 0

        return max(artifact_total, membership_total, stored_components)

    def _build_lineage_membership_index_payload(
        self,
        *,
        membership_index: dict[str, Any] | None,
        component_count_total: int,
        components: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stored_components = len(components)
        payload: dict[str, Any] = {
            "total_components": component_count_total,
            "stored_components": stored_components,
        }

        if stored_components == component_count_total and stored_components > 0:
            leaves: list[CompositeLeaf] = []
            for component in components:
                digest = component.get("component_digest")
                if not isinstance(digest, str) or not digest:
                    continue

                component_size = component.get("component_size")
                try:
                    if isinstance(component_size, bool):
                        normalized_component_size = int(component_size)
                    elif isinstance(component_size, int | float | str):
                        normalized_component_size = max(0, int(component_size))
                    else:
                        normalized_component_size = 0
                except (TypeError, ValueError):
                    normalized_component_size = 0

                component_type_raw = component.get("component_type")
                component_type = component_type_raw if isinstance(component_type_raw, str) else None
                leaf_kind_raw = component.get("leaf_kind")
                leaf_kind = leaf_kind_raw if isinstance(leaf_kind_raw, str) else "file"
                leaves.append(
                    CompositeLeaf(
                        relative_path=str(component.get("relative_path") or ""),
                        digest=digest.lower(),
                        size=normalized_component_size,
                        component_type=component_type,
                        leaf_kind=leaf_kind,
                    )
                )

            if len(leaves) == stored_components:
                bloom_payload = self._composite_builder._build_membership_index_base(leaves)
                payload["bloom_filter_base64"] = bloom_payload.get("bloom_filter_base64")
                payload["bloom_bits"] = bloom_payload.get("bloom_bits")
                payload["bloom_hashes"] = bloom_payload.get("bloom_hashes")
                payload["bloom_version"] = bloom_payload.get("bloom_version")
                return payload

        for key in ("bloom_filter_base64", "bloom_bits", "bloom_hashes", "bloom_version"):
            value = membership_index.get(key) if isinstance(membership_index, dict) else None
            if value is None:
                continue
            if key in {"bloom_bits", "bloom_hashes", "bloom_version"}:
                try:
                    payload[key] = int(value)
                except (TypeError, ValueError):
                    continue
                continue
            payload[key] = value

        required_bloom_keys = (
            "bloom_filter_base64",
            "bloom_bits",
            "bloom_hashes",
            "bloom_version",
        )
        if not all(key in payload and payload[key] is not None for key in required_bloom_keys):
            raise ValueError(
                "Lineage composite membership_index is missing required bloom fields; "
                "cannot register composite without a complete membership bloom index."
            )

        return payload

    @staticmethod
    def _extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
        return normalize_registration_hashes(artifact, fallback_to_hash=True)

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
