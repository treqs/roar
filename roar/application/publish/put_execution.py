"""
Put service orchestrator.

Executes the put workflow after the application layer has already
prepared session, git, and source context.

roar put ALWAYS registers lineage with GLaaS. This is not optional.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...application.publish.composite_builder import CompositeArtifactBuilder
from ...application.publish.composites import build_publish_composite_results
from ...application.publish.lineage import LineageCollector
from ...application.publish.metadata import build_put_operation_metadata_json
from ...application.publish.put_preparation import PreparedPutExecution
from ...application.publish.registration import (
    normalize_registration_hashes,
    normalize_registration_source_type,
    prepare_batch_registration_artifacts,
    register_publish_lineage,
    sync_publish_labels,
)
from ...application.publish.remote_job_uids import (
    build_remote_publication_job_uid,
    prepare_jobs_for_remote_publication,
)
from ...application.publish.session import build_staged_lineage_counts
from ...application.system_labels import refresh_job_system_labels
from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...db.context import DatabaseContext
from ...db.hashing import hash_files_blake3
from ...integrations.glaas.registration import RegistrationCoordinator
from ...integrations.storage.base import StorageBackend
from ...presenters.spinner import Spinner
from .job_links import (
    build_put_job_link_inputs,
    build_put_job_link_outputs,
    collect_local_composite_outputs,
    collect_local_lineage_composite_inputs,
)
from .put_composites import (
    preregister_put_lineage_composites_with_glaas,
    register_put_composites_with_glaas,
)
from .results import PutCompositeRegistration, PutDryRunItem, PutUploadedFile


def build_put_command(
    sources: list[str],
    destination: str | None,
    *,
    message: str,
    public: bool = True,
    anonymous: bool = False,
    no_tag: bool = False,
    as_dataset: bool = False,
    step_name: str | None = None,
) -> str:
    """Reconstruct a faithful ``roar put`` command string for the job record.

    The recorded command drives ``roar reproduce``, so it must include the
    destination and every provenance-relevant flag — not just the sources and
    message. Interaction/mode flags (``--yes``, ``--dry-run``) are intentionally
    omitted: they control confirmation/preview, not the published result.
    """
    parts = ["roar", "put"]
    if sources:
        parts.extend(shlex.quote(s) for s in sources)
    else:
        parts.append("(session outputs)")
    if destination:
        parts.append(shlex.quote(destination))
    if anonymous:
        parts.append("--anonymous")
    elif public is False:
        parts.append("--private")
    elif public:
        parts.append("--public")
    if no_tag:
        parts.append("--no-tag")
    if as_dataset:
        parts.append("--as-dataset")
    if step_name:
        parts.extend(["--step-name", shlex.quote(step_name)])
    parts.extend(["-m", shlex.quote(message)])
    return " ".join(parts)


def _upload_progress_message(done: int, total: int, uploaded_bytes: int) -> str:
    """Status line for the upload spinner, e.g. ``Uploading 12/100 files · 1.10 GB``."""
    return f"Uploading {done}/{total} files · {uploaded_bytes / 1e9:.2f} GB"


@dataclass
class PutResult:
    """Result of a put operation."""

    success: bool
    job_id: int | None = None
    job_uid: str | None = None
    session_hash: str | None = None
    session_url: str | None = None
    uploaded_files: list[PutUploadedFile] = field(default_factory=list)
    dry_run: bool = False
    would_upload: list[PutDryRunItem] = field(default_factory=list)
    composites_registered: list[PutCompositeRegistration] = field(default_factory=list)
    error: str | None = None


@dataclass
class _UploadedArtifact:
    """Tracks a single artifact through upload and registration."""

    local_path: str
    remote_url: str
    artifact_id: str
    hash: str


class PutService:
    """
    Orchestrates the put workflow.

    The application layer prepares the session/git/source context first.
    This service then performs upload, lineage collection, GLaaS registration,
    and local job/artifact persistence.
    """

    def __init__(
        self,
        db_context: DatabaseContext,
        backend: StorageBackend,
        destination: str,
        repo_root: Path | None = None,
        roar_dir: Path | None = None,
        lineage_collector: LineageCollector | None = None,
        registration_coordinator: RegistrationCoordinator | None = None,
        composite_builder: CompositeArtifactBuilder | None = None,
    ):
        """
        Initialize put service.

        Args:
            db_context: Database context for artifact/job operations.
            backend: Storage backend for uploads.
            destination: Destination URL (e.g., s3://bucket/prefix).
            repo_root: Repository root for path resolution.
            roar_dir: Path to .roar directory (for lineage collection).
            lineage_collector: Lineage collector (optional, for testing).
            registration_coordinator: Registration coordinator (optional, for testing).
            composite_builder: Composite builder (optional, for testing).
        """
        self._db = db_context
        self._backend = backend
        self._destination = destination
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._roar_dir = Path(roar_dir) if roar_dir else self._repo_root / ".roar"
        self._logger = get_logger()
        # Dependency injection for testing
        self._lineage_collector = lineage_collector
        self._registration_coordinator = registration_coordinator
        self._composite_builder = composite_builder or CompositeArtifactBuilder()

        self._logger.debug(
            "PutService initialized: destination=%s, repo_root=%s, roar_dir=%s, backend=%s",
            destination,
            self._repo_root,
            self._roar_dir,
            type(backend).__name__,
        )

    def put_prepared(
        self,
        *,
        prepared: PreparedPutExecution,
        sources: list[str],
        message: str,
        dry_run: bool = False,
        git_commit: str | None = None,
        git_tag: str | None = None,
        declared: bool = False,
        command: str | None = None,
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
            "put_prepared() called: sources=%s, message=%r, dry_run=%s, git_commit=%s, git_tag=%s",
            sources,
            message,
            dry_run,
            git_commit,
            git_tag,
        )
        client = prepared.glaas_client
        session_id = prepared.session_id
        session_hash = prepared.session_hash
        registration_session_id = prepared.registration_session_id
        registration_session_mode = prepared.registration_session_mode
        git_context = prepared.git_context
        resolved = prepared.resolved_sources
        destination_type = prepared.destination_type
        composite_source_type = prepared.composite_source_type
        dataset_identifiers = prepared.dataset_identifiers
        additional_composite_roots = prepared.additional_composite_roots

        self._logger.debug("Prepared session: id=%s, hash=%s", session_id, session_hash[:12])
        self._logger.debug(
            "Git context: repo=%s, commit=%s, branch=%s",
            git_context.repo,
            git_context.commit,
            git_context.branch,
        )
        self._logger.debug("Resolved %d source file(s)", len(resolved))

        if dry_run:
            self._logger.debug("Dry run mode — skipping upload and registration")
            return PutResult(
                success=True,
                session_hash=session_hash,
                session_url=prepared.session_url,
                dry_run=True,
                would_upload=[PutDryRunItem(path=str(r.path), exists=r.exists) for r in resolved],
            )

        # Process each file: hash, create artifact, upload
        uploads: list[_UploadedArtifact] = []
        composite_registrations: list[dict[str, Any]] = []
        lineage_composite_registrations: list[dict[str, Any]] = []
        with Spinner(f"Hashing {len(resolved)} file(s)..."):
            hashes_by_path = self._hash_files_batch([source.path for source in resolved])

        # Uploads are the long pole of a put (multi-GB artifacts to S3/GCS); show a
        # live N/M + cumulative-bytes counter rather than a dead terminal.
        total = len(resolved)
        uploaded_bytes = 0
        with Spinner(_upload_progress_message(0, total, 0)) as spin:
            for i, source in enumerate(resolved):
                file_path = source.path

                # Hashes are computed in one batch call up front.
                file_hash = hashes_by_path.get(str(file_path))
                if not file_hash:
                    raise OSError(f"Failed to hash file: {file_path}")
                self._logger.debug(
                    "File %d/%d: %s, blake3=%s",
                    i + 1,
                    total,
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

                uploads.append(
                    _UploadedArtifact(
                        local_path=str(file_path),
                        remote_url=remote_url,
                        artifact_id=artifact_id,
                        hash=file_hash,
                    )
                )
                uploaded_bytes += file_path.stat().st_size
                spin.update(_upload_progress_message(i + 1, total, uploaded_bytes))

        # Derive downstream views from the single uploads list
        uploaded_files = [
            PutUploadedFile(
                local_path=u.local_path,
                remote_url=u.remote_url,
                artifact_id=u.artifact_id,
                hash=u.hash,
            )
            for u in uploads
        ]
        artifact_urls = {u.artifact_id: u.remote_url for u in uploads}
        artifact_hashes = [u.hash for u in uploads]
        artifacts_info = [(u.artifact_id, u.local_path) for u in uploads]

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
        if registration_session_id:
            return self._put_prepared_with_registration_session(
                client=client,
                coordinator=coordinator,
                registration_session_id=registration_session_id,
                registration_session_mode=registration_session_mode,
                session_id=session_id,
                fallback_session_hash=session_hash or "",
                git_context=git_context,
                sources=sources,
                message=message,
                command=command,
                uploads=uploads,
                uploaded_files=uploaded_files,
                artifacts_info=artifacts_info,
                lineage=lineage,
                resolved=resolved,
                hashes_by_path=hashes_by_path,
                destination_type=destination_type,
                composite_source_type=composite_source_type,
                dataset_identifiers=dataset_identifiers,
                additional_composite_roots=additional_composite_roots,
                git_commit=git_commit,
                git_tag=git_tag,
                declared=declared,
            )

        with Spinner("Publishing lineage to GLaaS...") as spin:
            pre_registration_errors: list[str] = []
            spin.update("Registering lineage composites...")
            lineage_composite_registrations = preregister_put_lineage_composites_with_glaas(
                db_ctx=self._db,
                glaas_client=client,
                lineage_artifacts=lineage.artifacts,
                session_hash=session_hash or "",
                registration_errors=pre_registration_errors,
                dataset_identifiers=dataset_identifiers,
                composite_builder=self._composite_builder,
                logger=self._logger,
            )

            # Prepare artifacts for registration (add session_hash)
            uploaded_artifacts = self._build_uploaded_artifacts_for_registration(
                uploads,
                normalize_registration_source_type(destination_type),
            )
            prepared_artifacts = prepare_batch_registration_artifacts(
                uploaded_artifacts + lineage.artifacts,
                session_hash or "",
            )
            self._logger.debug("Prepared %d artifact(s) for registration", len(prepared_artifacts))

            self._logger.debug(
                "Registering lineage: session=%s, jobs=%d, artifacts=%d",
                session_hash[:12],
                len(lineage.jobs),
                len(prepared_artifacts),
            )
            spin.update("Registering jobs, artifacts, and links...")
            registration_result = register_publish_lineage(
                coordinator=coordinator,
                glaas_client=client,
                session_hash=session_hash or "",
                git_context=git_context,
                jobs=lineage.jobs,
                artifacts=prepared_artifacts,
                db_ctx=self._db,
                session_id=int(session_id),
                label_artifacts=[
                    *lineage.artifacts,
                    *[{"id": u.artifact_id, "hash": u.hash} for u in uploads],
                ],
                pre_registration_errors=pre_registration_errors,
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

            # Register directory composites after primitive artifacts exist in GLaaS.
            composite_results = build_publish_composite_results(
                resolved_sources=resolved,
                hashes_by_path=hashes_by_path,
                session_hash=session_hash or "",
                source_type=composite_source_type,
                additional_composite_roots=additional_composite_roots,
                composite_builder=self._composite_builder,
                declared=declared,
            )
            spin.update("Registering output composites...")
            composite_registrations = register_put_composites_with_glaas(
                db_ctx=self._db,
                glaas_client=client,
                composite_results=composite_results,
                registration_errors=registration_result.errors,
                dataset_identifiers=dataset_identifiers,
                logger=self._logger,
            )
        composite_result_items = [
            PutCompositeRegistration(
                root_path=(str(item["root_path"]) if item.get("root_path") is not None else None),
                hash=str(item["hash"]) if item.get("hash") is not None else None,
                component_count_stored=(
                    int(item["component_count_stored"])
                    if item.get("component_count_stored") is not None
                    else None
                ),
                component_count_total=(
                    int(item["component_count_total"])
                    if item.get("component_count_total") is not None
                    else None
                ),
                artifact_id=(
                    str(item["artifact_id"]) if item.get("artifact_id") is not None else None
                ),
                registered=bool(item["registered"]) if item.get("registered") is not None else None,
                local_persisted=(
                    bool(item["local_persisted"])
                    if item.get("local_persisted") is not None
                    else None
                ),
                local_error=(
                    str(item["local_error"]) if item.get("local_error") is not None else None
                ),
            )
            for item in composite_registrations
        ]

        # Build command string. Prefer the faithful command (destination + flags)
        # supplied by the caller; fall back to a destination-aware reconstruction.
        if command is None:
            command = build_put_command(sources, self._destination, message=message)

        # Build metadata
        metadata_json = build_put_operation_metadata_json(
            message=message,
            destination=self._destination,
            destination_type=destination_type,
            artifact_urls=artifact_urls,
            composite_registrations=composite_registrations,
            lineage_composite_registrations=lineage_composite_registrations,
            dataset_identifiers=dataset_identifiers,
            git_commit=git_commit,
            git_tag=git_tag,
            timestamp=time.time(),
        )

        # Create job record
        step_number = self._db.sessions.get_next_step_number(session_id)
        job_id, job_uid = self._db.jobs.create(
            command=command,
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            metadata=metadata_json,
            execution_backend="local",
            execution_role="host",
            job_type="put",
            exit_code=0,
        )
        refresh_job_system_labels(self._db, job_id=job_id)
        self._logger.debug(
            "Job created: id=%s, uid=%s, step=%d",
            job_id,
            job_uid,
            step_number,
        )

        session_hash_value = session_hash or ""
        self._link_local_put_job_artifacts(
            job_id=job_id,
            artifacts_info=artifacts_info,
            lineage_artifacts=lineage.artifacts,
            composite_registrations=composite_registrations,
        )
        with Spinner("Finalizing lineage links...") as spin:
            spin.update("Registering put job...")
            put_job_registered = self._register_put_job_with_glaas(
                coordinator=coordinator,
                command=command,
                session_hash=session_hash_value,
                job_uid=job_uid,
                git_context=git_context,
                step_number=step_number,
                metadata_json=metadata_json,
                registration_errors=registration_result.errors,
            )
            spin.update("Linking put job artifacts...")
            self._link_put_job_artifacts_with_glaas(
                coordinator=coordinator,
                session_hash=session_hash_value,
                job_uid=job_uid,
                uploaded_files=uploaded_files,
                lineage_artifacts=lineage.artifacts,
                composite_registrations=composite_registrations,
                registration_errors=registration_result.errors,
            )
            if put_job_registered:
                spin.update("Syncing put job labels...")
                self._sync_put_job_labels_with_glaas(
                    glaas_client=client,
                    session_hash=session_hash_value,
                    job_id=job_id,
                    job_uid=job_uid,
                    remote_job_uid=job_uid,
                    registration_errors=registration_result.errors,
                )

        registration_error = (
            "; ".join(registration_result.errors) if registration_result.errors else None
        )
        if registration_error:
            self._logger.debug("Registration errors: %s", registration_error)

        # Return result (treat any registration error as an unsuccessful registration pass)
        if registration_result.errors:
            self._logger.debug(
                (
                    "Put completed with registration errors: jobs_failed=%d, artifacts_failed=%d, "
                    "links_failed=%d, total_errors=%d"
                ),
                registration_result.jobs_failed,
                registration_result.artifacts_failed,
                registration_result.links_failed,
                len(registration_result.errors),
            )
            return PutResult(
                success=False,
                job_id=job_id,
                job_uid=job_uid,
                session_hash=session_hash,
                session_url=prepared.session_url,
                uploaded_files=uploaded_files,
                composites_registered=composite_result_items,
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
            session_url=prepared.session_url,
            uploaded_files=uploaded_files,
            composites_registered=composite_result_items,
        )

    def _put_prepared_with_registration_session(
        self,
        *,
        client: Any,
        coordinator: RegistrationCoordinator,
        registration_session_id: str,
        registration_session_mode: str | None,
        session_id: int,
        fallback_session_hash: str,
        git_context: GitContext,
        sources: list[str],
        message: str,
        command: str | None = None,
        uploads: list[_UploadedArtifact],
        uploaded_files: list[PutUploadedFile],
        artifacts_info: list[tuple[str, str]],
        lineage: Any,
        resolved: list[Any],
        hashes_by_path: dict[str, str],
        destination_type: str,
        composite_source_type: str | None,
        dataset_identifiers: list[dict[str, Any]],
        additional_composite_roots: dict[Path, list[Any]],
        git_commit: str | None,
        git_tag: str | None,
        declared: bool = False,
    ) -> PutResult:
        """Execute the authenticated Phase 5 publish flow via registration sessions."""
        session_hash = fallback_session_hash
        session_url: str | None = None
        registration_errors: list[str] = []
        lineage_composite_registrations: list[dict[str, Any]] = []
        composite_registrations: list[dict[str, Any]] = []
        if command is None:
            command = build_put_command(sources, self._destination, message=message)
        source_type = normalize_registration_source_type(destination_type)

        provisional_metadata_json = build_put_operation_metadata_json(
            message=message,
            destination=self._destination,
            destination_type=destination_type,
            artifact_urls={u.artifact_id: u.remote_url for u in uploads},
            composite_registrations=[],
            lineage_composite_registrations=[],
            dataset_identifiers=dataset_identifiers,
            git_commit=git_commit,
            git_tag=git_tag,
            timestamp=time.time(),
        )

        step_number = self._db.sessions.get_next_step_number(session_id)
        job_id, job_uid = self._db.jobs.create(
            command=command,
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            metadata=provisional_metadata_json,
            execution_backend="local",
            execution_role="host",
            job_type="put",
            exit_code=0,
        )
        self._logger.debug(
            "Put job created before registration-session finalize: id=%s, uid=%s, step=%d",
            job_id,
            job_uid,
            step_number,
        )
        remote_put_job_uid = build_remote_publication_job_uid(fallback_session_hash, job_uid)
        remote_lineage_jobs = prepare_jobs_for_remote_publication(
            lineage.jobs, fallback_session_hash
        )

        composite_results_for_linking = build_publish_composite_results(
            resolved_sources=resolved,
            hashes_by_path=hashes_by_path,
            session_hash=fallback_session_hash,
            source_type=composite_source_type,
            additional_composite_roots=additional_composite_roots,
            composite_builder=self._composite_builder,
            declared=declared,
        )

        put_job_registered = False
        put_job_links_succeeded = False
        with Spinner("Publishing lineage to GLaaS...") as spin:
            spin.update("Staging lineage jobs and artifacts...")
            registration_result = coordinator.register_lineage_under_registration_session(
                registration_session_id=registration_session_id,
                git_context=git_context,
                jobs=remote_lineage_jobs,
            )
            registration_errors.extend(registration_result.errors)

            if registration_result.jobs_failed == 0 and registration_result.links_failed == 0:
                spin.update("Staging put job...")
                put_job_result = coordinator.job_service.create_job_under_registration_session(
                    command=command,
                    timestamp=time.time(),
                    registration_session_id=registration_session_id,
                    job_uid=remote_put_job_uid,
                    git_commit=git_context.commit or "",
                    git_branch=git_context.branch or "",
                    duration_seconds=0.0,
                    exit_code=0,
                    job_type="put",
                    step_number=step_number,
                    metadata=provisional_metadata_json,
                )
                if not put_job_result.success:
                    if put_job_result.error:
                        registration_errors.append(f"Put job: {put_job_result.error}")
                else:
                    put_job_registered = True

                if put_job_registered:
                    spin.update("Linking put job artifacts...")
                    put_inputs = self._build_registration_session_put_job_inputs(
                        uploads=uploads,
                        lineage_artifacts=lineage.artifacts,
                        source_type=source_type,
                    )
                    put_outputs = self._build_registration_session_put_job_outputs(
                        composite_results_for_linking
                    )
                    link_result = (
                        coordinator.job_service.link_job_artifacts_under_registration_session(
                            registration_session_id=registration_session_id,
                            job_uid=remote_put_job_uid,
                            inputs=put_inputs,
                            outputs=put_outputs,
                        )
                    )
                    put_job_links_succeeded = link_result.success
                    if not link_result.success and link_result.error:
                        registration_errors.append(f"Put job links: {link_result.error}")

                if put_job_registered and put_job_links_succeeded:
                    spin.update("Finalizing lineage...")
                    expected_counts = None
                    if registration_session_mode == "anonymous_public":
                        expected_counts = build_staged_lineage_counts(
                            [
                                *remote_lineage_jobs,
                                {
                                    "job_uid": remote_put_job_uid,
                                    "_inputs": put_inputs,
                                    "_outputs": put_outputs,
                                },
                            ]
                        )
                    finalize_result = coordinator.session_service.finalize_registration_session(
                        registration_session_id=registration_session_id,
                        git_context=git_context,
                        expected_counts=expected_counts,
                    )
                    if not finalize_result.success:
                        registration_errors.append(
                            f"Registration session finalize failed: {finalize_result.error}"
                        )
                    else:
                        session_hash = finalize_result.session_hash
                        session_url = finalize_result.session_url

                        spin.update("Registering lineage composites...")
                        lineage_composite_registrations = (
                            preregister_put_lineage_composites_with_glaas(
                                db_ctx=self._db,
                                glaas_client=client,
                                lineage_artifacts=lineage.artifacts,
                                session_hash=session_hash,
                                registration_errors=registration_errors,
                                dataset_identifiers=dataset_identifiers,
                                composite_builder=self._composite_builder,
                                logger=self._logger,
                            )
                        )

                        composite_results = build_publish_composite_results(
                            resolved_sources=resolved,
                            hashes_by_path=hashes_by_path,
                            session_hash=session_hash,
                            source_type=composite_source_type,
                            additional_composite_roots=additional_composite_roots,
                            composite_builder=self._composite_builder,
                            declared=declared,
                        )
                        spin.update("Registering output composites...")
                        composite_registrations = register_put_composites_with_glaas(
                            db_ctx=self._db,
                            glaas_client=client,
                            composite_results=composite_results,
                            registration_errors=registration_errors,
                            dataset_identifiers=dataset_identifiers,
                            logger=self._logger,
                        )

        metadata_json = build_put_operation_metadata_json(
            message=message,
            destination=self._destination,
            destination_type=destination_type,
            artifact_urls={u.artifact_id: u.remote_url for u in uploads},
            composite_registrations=composite_registrations,
            lineage_composite_registrations=lineage_composite_registrations,
            dataset_identifiers=dataset_identifiers,
            git_commit=git_commit,
            git_tag=git_tag,
            timestamp=time.time(),
        )
        self._db.jobs.update_metadata(job_id, metadata_json)
        refresh_job_system_labels(self._db, job_id=job_id)
        self._link_local_put_job_artifacts(
            job_id=job_id,
            artifacts_info=artifacts_info,
            lineage_artifacts=lineage.artifacts,
            composite_registrations=composite_registrations,
        )

        if session_hash and put_job_registered:
            self._sync_put_job_labels_with_glaas(
                glaas_client=client,
                session_hash=session_hash,
                job_id=job_id,
                job_uid=job_uid,
                remote_job_uid=remote_put_job_uid,
                registration_errors=registration_errors,
            )

        composite_result_items = [
            PutCompositeRegistration(
                root_path=(str(item["root_path"]) if item.get("root_path") is not None else None),
                hash=str(item["hash"]) if item.get("hash") is not None else None,
                component_count_stored=(
                    int(item["component_count_stored"])
                    if item.get("component_count_stored") is not None
                    else None
                ),
                component_count_total=(
                    int(item["component_count_total"])
                    if item.get("component_count_total") is not None
                    else None
                ),
                artifact_id=(
                    str(item["artifact_id"]) if item.get("artifact_id") is not None else None
                ),
                registered=bool(item["registered"]) if item.get("registered") is not None else None,
                local_persisted=(
                    bool(item["local_persisted"])
                    if item.get("local_persisted") is not None
                    else None
                ),
                local_error=(
                    str(item["local_error"]) if item.get("local_error") is not None else None
                ),
            )
            for item in composite_registrations
        ]

        registration_error = "; ".join(registration_errors) if registration_errors else None
        if registration_error:
            self._logger.debug("Registration errors: %s", registration_error)
            return PutResult(
                success=False,
                job_id=job_id,
                job_uid=job_uid,
                session_hash=session_hash,
                session_url=session_url,
                uploaded_files=uploaded_files,
                composites_registered=composite_result_items,
                error=registration_error,
            )

        return PutResult(
            success=True,
            job_id=job_id,
            job_uid=job_uid,
            session_hash=session_hash,
            session_url=session_url,
            uploaded_files=uploaded_files,
            composites_registered=composite_result_items,
        )

    @staticmethod
    def _build_registration_session_put_job_inputs(
        *,
        uploads: list[_UploadedArtifact],
        lineage_artifacts: list[dict[str, Any]],
        source_type: str | None,
    ) -> list[dict[str, Any]]:
        """Build direct hash-addressed inputs for a staged put job."""
        inputs: list[dict[str, Any]] = []
        seen_rows: set[tuple[str, str]] = set()

        for uploaded in uploads:
            try:
                size = max(0, int(Path(uploaded.local_path).stat().st_size))
            except (OSError, TypeError, ValueError):
                size = 0
            row = {
                "artifact_hash": uploaded.hash,
                "path": uploaded.local_path,
                "size": size,
                "source_type": source_type,
            }
            row_key = (uploaded.hash, uploaded.local_path)
            if row_key not in seen_rows:
                seen_rows.add(row_key)
                inputs.append(row)

        for artifact in lineage_artifacts:
            if artifact.get("kind") != "composite":
                continue
            artifact_path = artifact.get("first_seen_path") or artifact.get("path")
            if not isinstance(artifact_path, str) or not artifact_path:
                continue
            hashes = normalize_registration_hashes(artifact)
            digest = hashes[0]["digest"] if hashes else None
            if not digest:
                continue
            row_key = (digest, artifact_path)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            inputs.append({"artifact_hash": digest, "path": artifact_path})

        return inputs

    @staticmethod
    def _build_registration_session_put_job_outputs(
        composite_results: list[Any],
    ) -> list[dict[str, Any]]:
        """Build direct hash-addressed outputs for a staged put job."""
        outputs: list[dict[str, Any]] = []
        seen_rows: set[tuple[str, str]] = set()
        for result in composite_results:
            digest = getattr(result, "digest", None)
            root_path = getattr(result, "root_path", None)
            if not isinstance(digest, str) or not digest:
                continue
            if not isinstance(root_path, str) or not root_path:
                continue
            row_key = (digest, root_path)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            outputs.append({"artifact_hash": digest, "path": root_path})
        return outputs

    @staticmethod
    def _build_uploaded_artifacts_for_registration(
        uploads: list[_UploadedArtifact],
        source_type: str | None,
    ) -> list[dict[str, Any]]:
        """Build registration payload entries for uploaded leaf artifacts."""
        payloads: list[dict[str, Any]] = []
        seen_digests: set[str] = set()

        for uploaded in uploads:
            digest = uploaded.hash.strip().lower()
            if not digest or digest in seen_digests:
                continue
            seen_digests.add(digest)

            size = 0
            try:
                size = max(0, int(Path(uploaded.local_path).stat().st_size))
            except (OSError, TypeError, ValueError):
                size = 0

            entry: dict[str, Any] = {
                "hashes": [{"algorithm": "blake3", "digest": digest}],
                "size": size,
                "source_type": source_type,
            }
            if uploaded.remote_url:
                entry["source_url"] = uploaded.remote_url

            payloads.append(entry)

        return payloads

    def _link_local_put_job_artifacts(
        self,
        job_id: int,
        artifacts_info: list[tuple[str, str]],
        lineage_artifacts: list[dict[str, Any]],
        composite_registrations: list[dict[str, Any]],
    ) -> None:
        """Persist local put job input/output edges for CLI lineage queries."""
        for artifact_id, path in artifacts_info:
            self._db.jobs.add_input(job_id, artifact_id, path)
        self._logger.debug("Linked %d artifact(s) as job inputs", len(artifacts_info))

        lineage_composite_inputs = collect_local_lineage_composite_inputs(lineage_artifacts)
        for artifact_id, path in lineage_composite_inputs:
            self._db.jobs.add_input(job_id, artifact_id, path)
        if lineage_composite_inputs:
            self._logger.debug(
                "Linked %d upstream composite input(s)",
                len(lineage_composite_inputs),
            )

        local_composite_outputs = collect_local_composite_outputs(composite_registrations)
        for artifact_id, path in local_composite_outputs:
            self._db.jobs.add_output(job_id, artifact_id, path)
        if local_composite_outputs:
            self._logger.debug(
                "Linked %d composite artifact(s) as job outputs",
                len(local_composite_outputs),
            )

        # get->put continuity: if any published file was got from HF (carries a
        # crosswalk sha256 matching a known dataset anchor), link the source anchor as
        # an input of the put job. This bridges the put's blake3 S3 composite to its
        # HF-source sha256 anchor at the job level — no sha256 computed at put, neither
        # composite mutated. Best-effort; never blocks a put.
        try:
            from .anchor_attribution import attribute_jobs_to_anchors

            attribute_jobs_to_anchors(db_ctx=self._db, job_ids=[job_id], logger=self._logger)
        except Exception as exc:  # pragma: no cover - defensive best effort
            self._logger.debug("put anchor attribution skipped: %s", exc)

    def _register_put_job_with_glaas(
        self,
        coordinator: RegistrationCoordinator,
        command: str,
        session_hash: str,
        job_uid: str,
        git_context: GitContext,
        step_number: int,
        metadata_json: str,
        registration_errors: list[str],
    ) -> bool:
        """Create the put sink node in GLaaS."""
        self._logger.debug("Registering put job with GLaaS: job_uid=%s, job_type=put", job_uid)
        put_job_result = coordinator.job_service.create_job(
            command=command,
            timestamp=time.time(),
            session_hash=session_hash,
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
                registration_errors.append(f"Put job: {put_job_result.error}")
            return False
        return True

    def _sync_put_job_labels_with_glaas(
        self,
        *,
        glaas_client: Any,
        session_hash: str,
        job_id: int,
        job_uid: str,
        remote_job_uid: str,
        registration_errors: list[str],
    ) -> None:
        """Sync the local current label document for the publish-time put job."""
        sync_publish_labels(
            glaas_client=glaas_client,
            db_ctx=self._db,
            session_id=None,
            session_hash=session_hash,
            jobs=[{"id": job_id, "job_uid": job_uid, "remote_job_uid": remote_job_uid}],
            artifacts=[],
            errors=registration_errors,
        )

    def _link_put_job_artifacts_with_glaas(
        self,
        coordinator: RegistrationCoordinator,
        session_hash: str,
        job_uid: str,
        uploaded_files: list[PutUploadedFile],
        lineage_artifacts: list[dict[str, Any]],
        composite_registrations: list[dict[str, Any]],
        registration_errors: list[str],
    ) -> None:
        """Link put job input/output edges in GLaaS after artifact registration."""
        input_artifacts, input_resolution_errors = build_put_job_link_inputs(
            coordinator=coordinator,
            uploaded_files=uploaded_files,
            lineage_artifacts=lineage_artifacts,
        )
        for resolution_error in input_resolution_errors:
            registration_errors.append(f"Put job input link: {resolution_error}")

        output_artifacts = build_put_job_link_outputs(composite_registrations)
        if not input_artifacts and not output_artifacts:
            return

        link_result = coordinator.job_service.link_job_artifacts(
            session_hash=session_hash,
            job_uid=job_uid,
            inputs=input_artifacts,
            outputs=output_artifacts,
        )
        if not link_result.success:
            self._logger.debug("Put job input linking failed: %s", link_result.error)
            if link_result.error:
                registration_errors.append(f"Put job links: {link_result.error}")

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
