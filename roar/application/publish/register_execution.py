"""
Register service for submitting artifact lineage to GLaaS.

Owns the registration mechanics after local lineage has already been collected.
"""

from __future__ import annotations

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
from .secrets import detect_lineage_secrets, filter_lineage_secrets

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

        registration_jobs = order_jobs_for_registration(
            normalize_jobs_for_registration(lineage.jobs)
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
        pre_registration_errors: list[str] = []
        with Spinner("Publishing lineage to GLaaS...") as spin:
            if has_lineage_composites(lineage.artifacts):
                spin.update("Registering composite artifacts...")
                try:
                    with create_database_context(roar_dir) as db_ctx:
                        composite_registrations = preregister_lineage_composites_with_glaas(
                            glaas_client=self.glaas_client,
                            db_ctx=db_ctx,
                            lineage_artifacts=lineage.artifacts,
                            session_hash=session_hash,
                            registration_errors=pre_registration_errors,
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

            refresh_job_artifact_references(lineage.jobs, lineage.artifacts)
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

    @staticmethod
    def _extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
        return normalize_registration_hashes(artifact, fallback_to_hash=True)
