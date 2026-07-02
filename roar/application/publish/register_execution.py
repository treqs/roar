"""
Register service for submitting artifact lineage to GLaaS.

Owns the registration mechanics after local lineage has already been collected.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.logging import get_logger
from ...filters.omit import OmitFilter
from ...integrations.config import config_get
from ...presenters.spinner import Spinner
from .job_preparation import (
    estimate_links,
    normalize_jobs_for_registration,
    order_jobs_for_registration,
)
from .remote_job_uids import prepare_jobs_for_remote_publication
from .secrets import (
    detect_lineage_secrets,
    filter_git_context_secrets,
    filter_lineage_secrets,
)
from .session import build_staged_lineage_counts

if TYPE_CHECKING:
    from ...application.publish.composite_builder import CompositeArtifactBuilder
    from ...application.publish.register_preparation import PreparedRegisterExecution
    from ...core.interfaces.lineage import LineageData
    from ...core.interfaces.logger import ILogger
    from ...integrations.glaas import GlaasClient
    from ...integrations.glaas.registration import RegistrationCoordinator


def create_database_context(roar_dir: Path) -> Any:
    """Load SQLAlchemy DB context only for non-dry-run registration paths."""
    from ...db.context import create_database_context as _create_database_context

    return _create_database_context(roar_dir)


def upgrade_s3_etags_to_blake3(*args: Any, **kwargs: Any) -> None:
    """Load S3 hash upgrade support only when requested."""
    from .blake3_upgrade import upgrade_s3_etags_to_blake3 as _upgrade_s3_etags_to_blake3

    _upgrade_s3_etags_to_blake3(*args, **kwargs)


def has_lineage_composites(*args: Any, **kwargs: Any) -> bool:
    """Load composite detection only for non-dry-run registration paths."""
    from .lineage_composites import has_lineage_composites as _has_lineage_composites

    return _has_lineage_composites(*args, **kwargs)


def preregister_lineage_composites_with_glaas(*args: Any, **kwargs: Any) -> Any:
    """Load composite preregistration only when needed."""
    from .lineage_composites import (
        preregister_lineage_composites_with_glaas as _preregister_lineage_composites_with_glaas,
    )

    return _preregister_lineage_composites_with_glaas(*args, **kwargs)


def refresh_job_artifact_references(*args: Any, **kwargs: Any) -> None:
    """Load heavy job/artifact reconciliation only for real registration."""
    from .job_preparation import (
        refresh_job_artifact_references as _refresh_job_artifact_references,
    )

    _refresh_job_artifact_references(*args, **kwargs)


def prepare_batch_registration_artifacts(*args: Any, **kwargs: Any) -> Any:
    """Load batch registration helpers only for real registration."""
    from ...application.publish.registration import (
        prepare_batch_registration_artifacts as _prepare_batch_registration_artifacts,
    )

    return _prepare_batch_registration_artifacts(*args, **kwargs)


def register_publish_lineage(*args: Any, **kwargs: Any) -> Any:
    """Load batch registration only for real registration."""
    from ...application.publish.registration import (
        register_publish_lineage as _register_publish_lineage,
    )

    return _register_publish_lineage(*args, **kwargs)


def sync_publish_labels(*args: Any, **kwargs: Any) -> Any:
    """Load label sync helpers only for real registration paths."""
    from ...application.publish.registration import sync_publish_labels as _sync_publish_labels

    return _sync_publish_labels(*args, **kwargs)


def normalize_registration_hashes(*args: Any, **kwargs: Any) -> Any:
    """Load hash normalization only when extracting registration payloads."""
    from ...application.publish.registration import (
        normalize_registration_hashes as _normalize_registration_hashes,
    )

    return _normalize_registration_hashes(*args, **kwargs)


@dataclass
class RegisterResult:
    """Result of register_artifact_lineage operation."""

    success: bool
    session_hash: str = ""
    session_url: str | None = None
    artifact_hash: str = ""
    jobs_registered: int = 0
    jobs_existing: int = 0
    artifacts_registered: int = 0
    links_created: int = 0
    labels_synced: int = 0
    already_registered: bool = False
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
        composite_builder: CompositeArtifactBuilder | None = None,
        omit_filter: OmitFilter | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the register service.

        Args:
            glaas_client: GLaaS client for API communication
            coordinator: Registration coordinator for 4-phase pattern
            omit_filter: Filter for detecting and redacting secrets
            logger: Logger instance. If None, uses the configured process logger.
        """
        self._glaas_client = glaas_client
        self._coordinator = coordinator
        self._composite_builder = composite_builder
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
        if self._glaas_client is None:
            from ...integrations.glaas import GlaasClient

            self._glaas_client = GlaasClient()
        return self._glaas_client

    @cached_property
    def coordinator(self) -> RegistrationCoordinator:
        """Get or create registration coordinator."""
        if self._coordinator is None:
            from ...integrations.glaas.registration import RegistrationCoordinator

            self._coordinator = RegistrationCoordinator()
        return self._coordinator

    @property
    def composite_builder(self) -> CompositeArtifactBuilder:
        """Get or create composite builder only for real registration paths."""
        if self._composite_builder is None:
            from ...application.publish.composite_builder import CompositeArtifactBuilder

            self._composite_builder = CompositeArtifactBuilder()
        return self._composite_builder

    def register_prepared_lineage(
        self,
        *,
        lineage: LineageData,
        roar_dir: Path,
        artifact_hash: str,
        dry_run: bool,
        as_blake3: bool,
        skip_confirmation: bool,
        confirm_callback: Callable[[list[str]], bool] | None,
        prepared: PreparedRegisterExecution,
    ) -> RegisterResult:
        """Register already-collected local lineage with GLaaS."""
        self._logger.debug(
            "Collected lineage: %d jobs, %d artifacts",
            len(lineage.jobs),
            len(lineage.artifacts),
        )
        git_context = prepared.git_context
        session_hash = prepared.session_hash
        session_id = prepared.session_id
        registration_session_id = prepared.registration_session_id
        registration_session_mode = prepared.registration_session_mode

        omit_filter = self.omit_filter
        detected_secrets: list[str] = []
        if omit_filter:
            detected_secrets = detect_lineage_secrets(
                lineage=lineage,
                git_context=git_context,
                omit_filter=omit_filter,
            )
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
            lineage = filter_lineage_secrets(
                lineage=lineage,
                omit_filter=omit_filter,
            )
            # The git repo URL is published at the session level (register /
            # finalize), so it must be redacted here too, not just detected.
            git_context, _ = filter_git_context_secrets(
                git_context=git_context,
                omit_filter=omit_filter,
            )

        registration_jobs = order_jobs_for_registration(
            normalize_jobs_for_registration(lineage.jobs)
        )
        remote_registration_jobs = (
            prepare_jobs_for_remote_publication(registration_jobs, session_hash)
            if registration_session_id
            else registration_jobs
        )

        if dry_run:
            return RegisterResult(
                success=True,
                session_hash=session_hash,
                artifact_hash=artifact_hash,
                jobs_registered=len(registration_jobs),
                artifacts_registered=len(lineage.artifacts),
                links_created=estimate_links(registration_jobs),
                secrets_detected=detected_secrets,
                secrets_redacted=bool(detected_secrets),
            )

        if as_blake3:
            upgrade_s3_etags_to_blake3(
                roar_dir=roar_dir,
                lineage=lineage,
                logger=self._logger,
            )

        composite_registrations: list[dict[str, Any]] = []
        registration_errors: list[str] = []
        finalized_session_hash = session_hash
        finalized_session_url = prepared.session_url
        finalize_failed = False
        already_registered = False
        with Spinner("Publishing lineage to GLaaS...") as spin:
            refresh_job_artifact_references(lineage.jobs, lineage.artifacts)

            if registration_session_id:
                spin.update("Staging jobs and artifacts...")
                staged_artifacts = prepare_batch_registration_artifacts(
                    lineage.artifacts,
                    registration_session_id,  # placeholder; client strips it before send
                    fallback_to_hash=True,
                    prefer_blake3_first=True,
                )
                batch_result = self.coordinator.register_lineage_under_registration_session(
                    registration_session_id=registration_session_id,
                    git_context=git_context,
                    jobs=remote_registration_jobs,
                    artifacts=staged_artifacts,
                )
                registration_errors.extend(batch_result.errors)

                if batch_result.already_registered_session_hash:
                    # Full re-register: this lineage is already on GLaaS. Skip
                    # finalize (nothing new to stage) and reuse the existing DAG,
                    # but still sync labels — the usual reason to re-register.
                    already_registered = True
                    finalized_session_hash = batch_result.already_registered_session_hash
                    finalized_session_url = None
                    if session_id is not None:
                        with create_database_context(roar_dir) as db_ctx:
                            batch_result.labels_synced = sync_publish_labels(
                                glaas_client=self.glaas_client,
                                db_ctx=db_ctx,
                                session_id=session_id,
                                session_hash=finalized_session_hash,
                                jobs=remote_registration_jobs,
                                artifacts=lineage.artifacts,
                                errors=registration_errors,
                            )
                elif batch_result.jobs_failed == 0 and batch_result.links_failed == 0:
                    spin.update("Finalizing lineage...")
                    finalize_result = (
                        self.coordinator.session_service.finalize_registration_session(
                            registration_session_id=registration_session_id,
                            git_context=git_context,
                            expected_counts=(
                                build_staged_lineage_counts(remote_registration_jobs)
                                if registration_session_mode == "anonymous_public"
                                else None
                            ),
                        )
                    )
                    if not finalize_result.success:
                        finalize_failed = True
                        registration_errors.append(
                            f"Registration session finalize failed: {finalize_result.error}"
                        )
                    else:
                        finalized_session_hash = finalize_result.session_hash
                        finalized_session_url = finalize_result.session_url

                        if has_lineage_composites(lineage.artifacts):
                            spin.update("Registering composite artifacts...")
                            try:
                                with create_database_context(roar_dir) as db_ctx:
                                    composite_registrations = (
                                        preregister_lineage_composites_with_glaas(
                                            glaas_client=self.glaas_client,
                                            db_ctx=db_ctx,
                                            lineage_artifacts=lineage.artifacts,
                                            session_hash=finalized_session_hash,
                                            registration_errors=registration_errors,
                                            composite_builder=self.composite_builder,
                                            logger=self._logger,
                                        )
                                    )
                                    if session_id is not None:
                                        batch_result.labels_synced = sync_publish_labels(
                                            glaas_client=self.glaas_client,
                                            db_ctx=db_ctx,
                                            session_id=session_id,
                                            session_hash=finalized_session_hash,
                                            jobs=remote_registration_jobs,
                                            artifacts=lineage.artifacts,
                                            errors=registration_errors,
                                        )
                            except Exception as e:
                                return RegisterResult(
                                    success=False,
                                    session_hash=finalized_session_hash,
                                    artifact_hash=artifact_hash,
                                    error=f"Composite artifact registration failed: {e}",
                                    secrets_detected=detected_secrets,
                                    secrets_redacted=bool(detected_secrets),
                                )
                        elif session_id is not None:
                            with create_database_context(roar_dir) as db_ctx:
                                batch_result.labels_synced = sync_publish_labels(
                                    glaas_client=self.glaas_client,
                                    db_ctx=db_ctx,
                                    session_id=session_id,
                                    session_hash=finalized_session_hash,
                                    jobs=remote_registration_jobs,
                                    artifacts=lineage.artifacts,
                                    errors=registration_errors,
                                )
            else:
                if has_lineage_composites(lineage.artifacts):
                    spin.update("Registering composite artifacts...")
                    try:
                        with create_database_context(roar_dir) as db_ctx:
                            composite_registrations = preregister_lineage_composites_with_glaas(
                                glaas_client=self.glaas_client,
                                db_ctx=db_ctx,
                                lineage_artifacts=lineage.artifacts,
                                session_hash=session_hash,
                                registration_errors=registration_errors,
                                composite_builder=self.composite_builder,
                                logger=self._logger,
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

                spin.update("Registering jobs, artifacts, and links...")

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
                registration_errors.extend(batch_result.errors)

        composite_registered = sum(1 for item in composite_registrations if item.get("registered"))
        composite_failed = sum(1 for item in composite_registrations if not item.get("registered"))
        total_artifacts_failed = batch_result.artifacts_failed + composite_failed
        # Report DISTINCT artifacts, matching `register --dry-run`'s
        # len(lineage.artifacts). The coordinator's running tally double-counts
        # (artifacts are tallied in both the staging and the link phase, and the
        # server's success count is per content-hash) — when everything
        # registered, the distinct figure is simply the lineage's artifact count.
        # The success gate below keys off *_failed, not this display number, so
        # this is safe. On partial failure, fall back to the coordinator's tally.
        if total_artifacts_failed == 0:
            total_artifacts_registered = len(lineage.artifacts)
        else:
            total_artifacts_registered = batch_result.artifacts_registered + composite_registered

        success = (
            batch_result.jobs_failed == 0 and total_artifacts_failed == 0 and not finalize_failed
        )
        if success and session_id is not None:
            try:
                with create_database_context(roar_dir) as db_ctx:
                    persist_glaas_publication_mapping(
                        db_ctx=db_ctx,
                        session_id=session_id,
                        prepared_session_hash=session_hash,
                        finalized_session_hash=finalized_session_hash,
                        jobs=remote_registration_jobs,
                    )
                    mark_lineage_synced(
                        db_ctx=db_ctx,
                        session_id=session_id,
                        jobs=lineage.jobs,
                        artifacts=lineage.artifacts,
                    )
            except Exception as exc:  # pragma: no cover - defensive durability best effort
                self._logger.warning(
                    "Failed to persist GLaaS publication mapping for session %s: %s",
                    session_id,
                    exc,
                )

        if registration_errors:
            self._logger.warning("Registration completed with errors: %s", registration_errors)

        return RegisterResult(
            success=success,
            session_hash=finalized_session_hash,
            session_url=finalized_session_url,
            artifact_hash=artifact_hash,
            jobs_registered=batch_result.jobs_created,
            jobs_existing=batch_result.jobs_existing,
            artifacts_registered=total_artifacts_registered,
            links_created=batch_result.links_created,
            labels_synced=batch_result.labels_synced,
            already_registered=already_registered,
            error="; ".join(registration_errors) if registration_errors else None,
            secrets_detected=detected_secrets,
            secrets_redacted=bool(detected_secrets),
        )

    @staticmethod
    def _extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
        return normalize_registration_hashes(artifact, fallback_to_hash=True)


def mark_lineage_synced(
    *,
    db_ctx: Any,
    session_id: int,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    now: float | None = None,
) -> None:
    """Stamp ``synced_at`` on the session, jobs, and artifacts pushed to GLaaS.

    Called only on a fully-successful register (no failed jobs/artifacts), so the
    whole registered lineage is known to be on GLaaS. This is what makes
    ``roar db``'s "synced" counts reflect reality.
    """
    synced_at = time.time() if now is None else now
    db_ctx.sessions.mark_synced([session_id], synced_at)
    db_ctx.jobs.mark_synced([j["id"] for j in jobs if isinstance(j.get("id"), int)], synced_at)
    db_ctx.artifacts.mark_synced(
        [a["id"] for a in artifacts if isinstance(a.get("id"), str) and a.get("id")],
        synced_at,
    )


def persist_glaas_publication_mapping(
    *,
    db_ctx: Any,
    session_id: int,
    prepared_session_hash: str,
    finalized_session_hash: str,
    jobs: list[dict[str, Any]],
) -> None:
    """Persist local-to-remote GLaaS publication identity for later label sync."""
    session = db_ctx.sessions.get(session_id)
    if not isinstance(session, dict):
        return

    metadata = _load_session_metadata(session.get("metadata"))
    roar_metadata = metadata.setdefault("roar", {})
    if not isinstance(roar_metadata, dict):
        roar_metadata = {}
        metadata["roar"] = roar_metadata
    remote_publication = roar_metadata.setdefault("remote_publication", {})
    if not isinstance(remote_publication, dict):
        remote_publication = {}
        roar_metadata["remote_publication"] = remote_publication

    remote_publication["glaas"] = {
        "schema_version": 1,
        "session_hash": finalized_session_hash,
        "prepared_session_hash": prepared_session_hash,
        "published_at": time.time(),
        "jobs": _remote_job_uid_mapping(jobs),
    }
    db_ctx.sessions.update_metadata(
        session_id,
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )


def _load_session_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {"raw": raw_metadata}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}


def _remote_job_uid_mapping(jobs: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for job in jobs:
        local_job_uid = job.get("job_uid") or job.get("local_job_uid") or job.get("source_job_uid")
        remote_job_uid = job.get("remote_job_uid") or job.get("job_uid")
        if not local_job_uid or not remote_job_uid:
            continue
        mapping[str(local_job_uid)] = str(remote_job_uid)
    return mapping
