"""
Put service orchestrator.

Executes the put workflow after the application layer has already
prepared session, git, and source context.

roar put ALWAYS registers lineage with GLaaS. This is not optional.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from ...application.publish.composite_builder import CompositeArtifactBuilder, CompositeBuildResult
from ...application.publish.lineage import LineageCollector
from ...application.publish.put_preparation import PreparedPutExecution
from ...application.publish.registration import (
    CompositeRegistrationCandidate,
    build_lineage_composite_candidate,
    extract_composite_digest,
    normalize_lineage_component_rows,
    normalize_registration_hashes,
    normalize_registration_source_type,
    parse_composite_registration_response,
    prepare_batch_registration_artifacts,
    preregister_lineage_composites,
    register_publish_lineage,
    resolve_lineage_component_count_total,
)
from ...application.publish.source_resolution import ResolvedSource
from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...db.context import optional_repo
from ...glaas_client import GlaasClient
from ...integrations.storage.base import StorageBackend
from ...services.execution.dataset_identifier import DatasetIdentifierInferer
from ...services.registration import (
    RegistrationCoordinator,
    _artifact_ref,
)
from ...services.registration._dataset_label import build_dataset_metadata, find_matching_identifier
from ...services.registration._dataset_profile import build_dataset_profile
from ...services.transfer import (
    DatabaseContext,
    build_operation_metadata_json,
    hash_files_blake3,
)
from .job_links import (
    build_put_job_link_inputs,
    build_put_job_link_outputs,
    collect_local_composite_outputs,
    collect_local_lineage_composite_inputs,
)

_AUTO_COMPOSITE_MIN_CONFIDENCE = 0.80

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
    composites_registered: list[dict[str, Any]] = field(default_factory=list)
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
        dataset_identifier_inferer: DatasetIdentifierInferer | None = None,
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
            dataset_identifier_inferer: Dataset identifier inferer (optional, for testing).
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
        self._dataset_identifier_inferer = dataset_identifier_inferer or DatasetIdentifierInferer()

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
        git_context = prepared.git_context
        resolved = prepared.resolved_sources
        destination_type = prepared.destination_type
        composite_source_type = prepared.composite_source_type

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
                would_upload=[{"path": str(r.path), "exists": r.exists} for r in resolved],
            )

        # Process each file: hash, create artifact, upload
        uploads: list[_UploadedArtifact] = []
        composite_registrations: list[dict[str, Any]] = []
        lineage_composite_registrations: list[dict[str, Any]] = []
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

            uploads.append(
                _UploadedArtifact(
                    local_path=str(file_path),
                    remote_url=remote_url,
                    artifact_id=artifact_id,
                    hash=file_hash,
                )
            )

        # Derive downstream views from the single uploads list
        uploaded_files = [
            {
                "local_path": u.local_path,
                "remote_url": u.remote_url,
                "artifact_id": u.artifact_id,
                "hash": u.hash,
            }
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
        pre_registration_errors: list[str] = []
        dataset_identifiers = self._infer_dataset_identifiers(sources, resolved)

        lineage_composite_registrations = self._register_lineage_composites_with_glaas(
            client=client,
            lineage_artifacts=lineage.artifacts,
            session_hash=session_hash or "",
            registration_errors=pre_registration_errors,
            dataset_identifiers=dataset_identifiers,
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
        composite_results = self._build_composite_payloads(
            resolved=resolved,
            hashes_by_path=hashes_by_path,
            session_hash=session_hash or "",
            source_type=composite_source_type,
            dataset_identifiers=dataset_identifiers,
        )
        composite_registrations = self._register_composites_with_glaas(
            client,
            composite_results,
            registration_result.errors,
            dataset_identifiers=dataset_identifiers,
        )

        # Build command string
        source_str = " ".join(sources) if sources else "(session outputs)"
        command = f'roar put {source_str} -m "{message}"'

        # Build metadata
        metadata_json = self._build_put_metadata(
            message=message,
            destination_type=destination_type,
            artifact_urls=artifact_urls,
            composite_registrations=composite_registrations,
            lineage_composite_registrations=lineage_composite_registrations,
            dataset_identifiers=dataset_identifiers,
            git_commit=git_commit,
            git_tag=git_tag,
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
        self._register_put_job_with_glaas(
            coordinator=coordinator,
            command=command,
            session_hash=session_hash_value,
            job_uid=job_uid,
            git_context=git_context,
            step_number=step_number,
            metadata_json=metadata_json,
            registration_errors=registration_result.errors,
        )
        self._link_put_job_artifacts_with_glaas(
            coordinator=coordinator,
            session_hash=session_hash_value,
            job_uid=job_uid,
            uploaded_files=uploaded_files,
            lineage_artifacts=lineage.artifacts,
            composite_registrations=composite_registrations,
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
                composites_registered=composite_registrations,
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
            composites_registered=composite_registrations,
        )

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

    def _register_lineage_composites_with_glaas(
        self,
        client: GlaasClient,
        lineage_artifacts: list[dict[str, Any]],
        session_hash: str,
        registration_errors: list[str],
        dataset_identifiers: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Pre-register lineage composite artifacts before coordinator link phase.

        Coordinator phase-4 link resolution requires composite hashes to exist in
        GLaaS. These lineage composites are not sent through the batch artifacts
        endpoint because that endpoint cannot persist component metadata.
        """
        payloads = self._build_lineage_composite_payloads(
            lineage_artifacts=lineage_artifacts,
            session_hash=session_hash,
            dataset_identifiers=dataset_identifiers,
        )
        return preregister_lineage_composites(
            glaas_client=client,
            payloads=payloads,
            registration_errors=registration_errors,
            logger=self._logger,
        )

    def _build_lineage_composite_payloads(
        self,
        lineage_artifacts: list[dict[str, Any]],
        session_hash: str,
        dataset_identifiers: list[dict[str, Any]] | None = None,
    ) -> list[CompositeRegistrationCandidate]:
        """Build composite registration payloads from local lineage artifacts."""
        composites_repo = cast(Any, optional_repo(self._db, "composites"))
        payloads: list[CompositeRegistrationCandidate] = []
        seen_hashes: set[str] = set()

        for artifact in lineage_artifacts:
            hashes = self._extract_registration_hashes(artifact)
            composite_digest = extract_composite_digest(hashes)
            if not composite_digest:
                continue
            if composite_digest in seen_hashes:
                continue

            component_rows: list[dict[str, Any]] = []
            membership_index: dict[str, Any] | None = None
            artifact_id = artifact.get("id")
            if composites_repo is not None and isinstance(artifact_id, str) and artifact_id:
                try:
                    rows = composites_repo.get_components(artifact_id, limit=5000)
                    if isinstance(rows, list):
                        component_rows = [row for row in rows if isinstance(row, dict)]
                except Exception as exc:  # pragma: no cover - defensive path
                    self._logger.warning(
                        "Failed to load local components for lineage composite %s: %s",
                        composite_digest[:12],
                        exc,
                    )
                try:
                    raw_membership = composites_repo.get_membership_index(artifact_id)
                    if isinstance(raw_membership, dict):
                        membership_index = raw_membership
                except Exception as exc:  # pragma: no cover - defensive path
                    self._logger.warning(
                        "Failed to load local membership index for lineage composite %s: %s",
                        composite_digest[:12],
                        exc,
                    )

            components = normalize_lineage_component_rows(
                component_rows,
                resolve_component=self._resolve_lineage_component_for_registration,
                logger=self._logger,
            )

            seen_hashes.add(composite_digest)
            metadata_json = self._build_composite_dataset_metadata_json(
                root_path=str(_artifact_ref.artifact_path(artifact) or ""),
                dataset_identifiers=dataset_identifiers,
                artifact_metadata=artifact.get("metadata"),
                components=components,
                component_count_total=resolve_lineage_component_count_total(
                    artifact_component_count=artifact.get("component_count"),
                    membership_index=membership_index,
                    stored_components=len(components),
                ),
            )
            candidate = build_lineage_composite_candidate(
                artifact=artifact,
                composite_digest=composite_digest,
                hashes=hashes,
                components=components,
                membership_index=membership_index,
                session_hash=session_hash,
                composite_builder=self._composite_builder,
                metadata=metadata_json,
                logger=self._logger,
            )
            if candidate is None:
                continue
            payloads.append(candidate)

        return payloads

    @staticmethod
    def _resolve_lineage_component_for_registration(
        row: dict[str, Any],
    ) -> tuple[str, str] | None:
        component_algorithm = row.get("component_algorithm")
        component_digest = row.get("component_digest")
        if (
            isinstance(component_algorithm, str)
            and component_algorithm.strip()
            and isinstance(component_digest, str)
            and component_digest
        ):
            return component_algorithm.strip().lower(), component_digest.lower()
        return None

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
    ) -> None:
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

    def _link_put_job_artifacts_with_glaas(
        self,
        coordinator: RegistrationCoordinator,
        session_hash: str,
        job_uid: str,
        uploaded_files: list[dict[str, Any]],
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

    @staticmethod
    def _extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
        return normalize_registration_hashes(artifact)

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

    def _build_composite_payloads(
        self,
        resolved: list[ResolvedSource],
        hashes_by_path: dict[str, str],
        session_hash: str,
        source_type: str | None,
        dataset_identifiers: list[dict[str, Any]],
    ) -> list[CompositeBuildResult]:
        grouped_by_root: dict[Path, list[ResolvedSource]] = {}
        for source in resolved:
            if source.source_root is None:
                continue
            grouped_by_root.setdefault(source.source_root, []).append(source)

        for root_path, sources in self._detect_additional_composite_roots(
            resolved,
            dataset_identifiers,
        ).items():
            grouped_by_root.setdefault(root_path, sources)

        results: list[CompositeBuildResult] = []
        for root_path in sorted(grouped_by_root, key=lambda item: str(item)):
            result = self._composite_builder.build_for_root(
                root_path=root_path,
                resolved_sources=grouped_by_root[root_path],
                hashes_by_path=hashes_by_path,
                session_hash=session_hash,
                source_type=source_type,
            )
            if result:
                results.append(result)
        return results

    def _infer_dataset_identifiers(
        self,
        source_specs: list[str],
        resolved: list[ResolvedSource],
    ) -> list[dict[str, Any]]:
        paths: list[str] = []

        for source in source_specs:
            if "://" in source:
                paths.append(source)
                continue

            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = self._repo_root / source_path
            paths.append(os.path.abspath(str(source_path)))

        for item in resolved:
            paths.append(str(item.path))
            if item.source_root is not None:
                paths.append(str(item.source_root))

        unique_paths = [path for path in dict.fromkeys(paths) if path]
        return self._dataset_identifier_inferer.infer(
            unique_paths,
            repo_root=str(self._repo_root),
        )

    def _detect_additional_composite_roots(
        self,
        resolved: list[ResolvedSource],
        dataset_identifiers: list[dict[str, Any]],
    ) -> dict[Path, list[ResolvedSource]]:
        ungrouped = [source for source in resolved if source.source_root is None]
        if len(ungrouped) < 2:
            return {}

        grouped: dict[Path, list[ResolvedSource]] = {}
        assigned_paths: set[str] = set()

        for candidate in dataset_identifiers:
            confidence = candidate.get("confidence")
            if not isinstance(confidence, (int, float)):
                continue
            if float(confidence) < _AUTO_COMPOSITE_MIN_CONFIDENCE:
                continue

            dataset_id = str(candidate.get("dataset_id", ""))
            parsed = urlparse(dataset_id)
            if parsed.scheme != "file" or not parsed.path:
                continue

            root = Path(parsed.path)
            matches = [
                source
                for source in ungrouped
                if str(source.path) not in assigned_paths and source.path.is_relative_to(root)
            ]
            if len(matches) < 2:
                continue

            grouped[root] = matches
            assigned_paths.update(str(source.path) for source in matches)

        fallback_by_parent: dict[Path, list[ResolvedSource]] = {}
        for source in ungrouped:
            if str(source.path) in assigned_paths:
                continue
            fallback_by_parent.setdefault(source.path.parent, []).append(source)

        for parent, sources in fallback_by_parent.items():
            if len(sources) >= 2:
                grouped[parent] = sources

        return grouped

    def _persist_local_composite_registration(
        self,
        composite: CompositeBuildResult,
        composite_registration: dict[str, Any],
        dataset_identifiers: list[dict[str, Any]] | None = None,
    ) -> str | None:
        artifacts_repo = cast(Any, optional_repo(self._db, "artifacts"))
        composites_repo = cast(Any, optional_repo(self._db, "composites"))
        if artifacts_repo is None or composites_repo is None:
            return None

        try:
            meta_dict: dict[str, Any] = {
                "composite": {
                    "root_path": composite.root_path,
                    "component_count_total": composite.component_count_total,
                    "component_count_stored": composite.component_count_stored,
                }
            }
            profile = build_dataset_profile(
                list(composite.payload.get("components") or []),
                total_components=composite.component_count_total,
            )
            if dataset_identifiers:
                matching = find_matching_identifier(composite.root_path, dataset_identifiers)
                if matching is not None:
                    meta_dict["dataset"] = build_dataset_metadata(matching)
            if profile is not None:
                dataset_meta = meta_dict.setdefault("dataset", {})
                if isinstance(dataset_meta, dict):
                    dataset_meta["profile"] = profile
            metadata = json.dumps(meta_dict)
            local_artifact_id, _created = artifacts_repo.register(
                hashes={"composite-blake3": composite.digest},
                size=int(composite.payload.get("size") or 0),
                path=composite.root_path,
                source_type=composite.payload.get("source_type"),
                metadata=metadata,
            )
            composites_repo.upsert_details(
                artifact_id=local_artifact_id,
                components=list(composite.payload.get("components") or []),
                component_count_total=composite.component_count_total,
                membership_index=composite.payload.get("membership_index"),
            )
            composite_registration["local_artifact_id"] = local_artifact_id
            return None
        except Exception as exc:  # pragma: no cover - defensive best effort
            self._logger.warning(
                "Local composite persistence failed for %s: %s",
                composite.digest[:12],
                exc,
            )
            return str(exc)

    def _build_composite_dataset_metadata_json(
        self,
        root_path: str,
        dataset_identifiers: list[dict[str, Any]] | None,
        artifact_metadata: Any = None,
        components: list[dict[str, Any]] | None = None,
        component_count_total: int | None = None,
    ) -> str | None:
        """Build serialized composite metadata containing a normalized dataset label."""
        dataset_metadata = self._extract_dataset_metadata_from_artifact_metadata(artifact_metadata)
        derived_profile = build_dataset_profile(
            components or [],
            total_components=component_count_total,
        )

        if dataset_metadata is None and dataset_identifiers:
            matching = find_matching_identifier(root_path, dataset_identifiers)
            if matching is not None:
                extracted = build_dataset_metadata(matching)
                if extracted:
                    dataset_metadata = extracted

        if dataset_metadata is None and derived_profile is None:
            return None

        if dataset_metadata is None:
            dataset_metadata = {}

        if derived_profile is not None:
            dataset_metadata["profile"] = derived_profile

        return json.dumps({"dataset": dataset_metadata}, separators=(",", ":"))

    @staticmethod
    def _extract_dataset_metadata_from_artifact_metadata(
        artifact_metadata: Any,
    ) -> dict[str, Any] | None:
        parsed_metadata: dict[str, Any] | None = None

        if isinstance(artifact_metadata, dict):
            parsed_metadata = artifact_metadata
        elif isinstance(artifact_metadata, str):
            try:
                parsed = json.loads(artifact_metadata)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                parsed_metadata = parsed

        if parsed_metadata is None:
            return None

        dataset = parsed_metadata.get("dataset")
        if not isinstance(dataset, dict):
            return None

        normalized = build_dataset_metadata(dataset)
        if not normalized:
            return None
        return normalized

    def _register_composites_with_glaas(
        self,
        client: GlaasClient,
        composite_results: list[CompositeBuildResult],
        registration_errors: list[str],
        dataset_identifiers: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Register composite artifacts with GLaaS and persist locally."""
        composite_registrations: list[dict[str, Any]] = []
        for composite in composite_results:
            payload = dict(composite.payload)
            metadata_json = self._build_composite_dataset_metadata_json(
                root_path=composite.root_path,
                dataset_identifiers=dataset_identifiers,
                components=list(composite.payload.get("components") or []),
                component_count_total=composite.component_count_total,
            )
            if metadata_json is not None:
                payload["metadata"] = metadata_json
            response = client.register_composite_artifact(payload)
            result, error = parse_composite_registration_response(response)

            composite_registration = {
                "root_path": composite.root_path,
                "hash": composite.digest,
                "component_count_total": composite.component_count_total,
                "component_count_stored": composite.component_count_stored,
            }
            if error:
                composite_registration["registered"] = False
                composite_registration["error"] = error
                registration_errors.append(f"Composite {composite.digest[:12]}: {error}")
            else:
                composite_registration["registered"] = True
                if isinstance(result, dict):
                    if "artifact_id" in result:
                        composite_registration["artifact_id"] = result["artifact_id"]
                    if "created" in result:
                        composite_registration["created"] = result["created"]
                local_persist_error = self._persist_local_composite_registration(
                    composite,
                    composite_registration,
                    dataset_identifiers=dataset_identifiers,
                )
                if local_persist_error:
                    composite_registration["local_persisted"] = False
                    composite_registration["local_error"] = local_persist_error
                else:
                    composite_registration["local_persisted"] = True
            composite_registrations.append(composite_registration)
        return composite_registrations

    def _build_put_metadata(
        self,
        message: str,
        destination_type: str,
        artifact_urls: dict[str, str],
        composite_registrations: list[dict[str, Any]],
        lineage_composite_registrations: list[dict[str, Any]],
        dataset_identifiers: list[dict[str, Any]],
        git_commit: str | None,
        git_tag: str | None,
    ) -> str:
        """Build the metadata JSON payload for the put job record."""
        metadata_json = build_operation_metadata_json(
            "put",
            {
                "message": message,
                "destination": self._destination,
                "destination_type": destination_type,
                "artifacts": artifact_urls,
                "composites": composite_registrations,
                "lineage_composites": lineage_composite_registrations,
                "dataset_identifiers": dataset_identifiers,
                "git_commit": git_commit,
                "git_tag": git_tag,
                "timestamp": time.time(),
            },
        )
        self._logger.debug(
            "Job metadata: destination_type=%s, artifacts=%d", destination_type, len(artifact_urls)
        )
        return metadata_json
