"""
Registration coordinator service.

Orchestrates the 4-phase registration pattern:
1. Session - Register session with git context
2. Jobs - Create jobs without artifact links
3. Artifacts - Register all artifacts
4. Link Artifacts - Link job inputs/outputs to artifacts
"""

from functools import cached_property
from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.registration import (
    BatchRegistrationResult,
    GitContext,
    IRegistrationCoordinator,
)
from ....core.logging import get_logger
from . import _artifact_ref
from .artifact import ArtifactRegistrationService
from .job import JobRegistrationService
from .session import SessionRegistrationService


class RegistrationCoordinator(IRegistrationCoordinator):
    """
    Coordinates registration in the correct order.

    This service enforces the 4-phase registration pattern to ensure
    FK constraints are satisfied:
    1. Session already registered (passed as session_hash)
    2. Create all jobs (without I/O)
    3. Register all artifacts
    4. Link job I/O for each job

    Replaces the inline registration logic in put.py:391-572.
    """

    def __init__(
        self,
        session_service: SessionRegistrationService | None = None,
        artifact_service: ArtifactRegistrationService | None = None,
        job_service: JobRegistrationService | None = None,
        logger: ILogger | None = None,
    ):
        """
        Initialize the registration coordinator.

        Args:
            session_service: Session registration service
            artifact_service: Artifact registration service
            job_service: Job registration service
            logger: Logger instance. If None, uses the configured process logger.
        """
        self._session_service = session_service
        self._artifact_service = artifact_service
        self._job_service = job_service

        self._logger = logger or get_logger()

    @cached_property
    def session_service(self) -> SessionRegistrationService:
        """Get or create session service."""
        return self._session_service or SessionRegistrationService()

    @cached_property
    def artifact_service(self) -> ArtifactRegistrationService:
        """Get or create artifact service."""
        return self._artifact_service or ArtifactRegistrationService()

    @cached_property
    def job_service(self) -> JobRegistrationService:
        """Get or create job service."""
        return self._job_service or JobRegistrationService()

    def register_lineage(
        self,
        session_hash: str,
        git_context: GitContext,
        jobs: list[dict],
        artifacts: list[dict],
    ) -> BatchRegistrationResult:
        """
        Register complete lineage with correct ordering.

        Implements the 4-phase registration pattern:
        1. Session already registered (passed as session_hash)
        2. Create all jobs (without I/O) - Phase 2
        3. Register all artifacts - Phase 3
        4. Link job I/O for each job - Phase 4

        Args:
            session_hash: Pre-computed session hash (session already registered)
            git_context: Git context for the session
            jobs: List of job dicts with _inputs and _outputs for linking
            artifacts: List of artifact dicts to register

        Returns:
            BatchRegistrationResult with counts and errors
        """
        errors: list[str] = []
        jobs_created = 0
        jobs_failed = 0
        artifacts_registered = 0
        artifacts_failed = 0
        links_created = 0
        links_failed = 0

        self._logger.debug(
            "Starting lineage registration: session=%s, jobs=%d, artifacts=%d",
            session_hash[:12],
            len(jobs),
            len(artifacts),
        )

        # Phase 2: Create all jobs WITHOUT artifact links (batch)
        self._logger.debug("Phase 2: Batch-creating %d jobs without artifact links", len(jobs))
        job_uids_created = []

        if jobs:
            batch_results = self.job_service.create_jobs_batch(
                jobs=jobs,
                session_hash=session_hash,
                git_context=git_context,
            )
            for result in batch_results:
                if result.success:
                    jobs_created += 1
                    job_uids_created.append(result.job_uid)
                else:
                    jobs_failed += 1
                    if result.error:
                        errors.append(f"Job {result.job_uid}: {result.error}")

        self._logger.debug(
            "Phase 2 complete: %d jobs created, %d failed",
            jobs_created,
            jobs_failed,
        )

        # Phase 3: Register all artifacts
        self._logger.debug("Phase 3: Registering %d artifacts", len(artifacts))

        if artifacts:
            art_result = self.artifact_service.register_batch(artifacts, session_hash)
            artifacts_registered = art_result.success_count
            artifacts_failed = art_result.error_count
            errors.extend(art_result.errors)

        self._logger.debug(
            "Phase 3 complete: %d artifacts registered, %d failed",
            artifacts_registered,
            artifacts_failed,
        )

        # Phase 4: Link job artifacts
        self._logger.debug("Phase 4: Linking artifacts to %d jobs", len(job_uids_created))
        artifact_hash_cache: dict[str, str] = {}

        for job in jobs:
            job_uid = job.get("job_uid")
            if not job_uid or job_uid not in job_uids_created:
                continue

            # Get inputs/outputs in {hash, path} format
            inputs = self._extract_io_list(job, "_inputs", "_input_hashes")
            outputs = self._extract_io_list(job, "_outputs", "_output_hashes")

            if not inputs and not outputs:
                continue

            resolved_inputs, input_resolution_errors = self._resolve_io_artifact_hashes(
                inputs,
                artifact_hash_cache,
                "input",
            )
            resolved_outputs, output_resolution_errors = self._resolve_io_artifact_hashes(
                outputs,
                artifact_hash_cache,
                "output",
            )
            resolution_errors = input_resolution_errors + output_resolution_errors
            if resolution_errors:
                links_failed += 1
                errors.extend([f"Link {job_uid}: {msg}" for msg in resolution_errors])

            if not resolved_inputs and not resolved_outputs:
                continue

            link_result = self.job_service.link_job_artifacts(
                session_hash=session_hash,
                job_uid=job_uid,
                inputs=resolved_inputs,
                outputs=resolved_outputs,
            )

            if link_result.success:
                links_created += link_result.inputs_linked + link_result.outputs_linked
            else:
                links_failed += 1
                if link_result.error:
                    errors.append(f"Link {job_uid}: {link_result.error}")

        self._logger.debug(
            "Phase 4 complete: %d links created, %d failed",
            links_created,
            links_failed,
        )

        self._logger.debug(
            "Lineage registration complete: jobs=%d/%d, artifacts=%d/%d, links=%d",
            jobs_created,
            jobs_created + jobs_failed,
            artifacts_registered,
            artifacts_registered + artifacts_failed,
            links_created,
        )

        return BatchRegistrationResult(
            session_registered=True,  # Session was already registered
            jobs_created=jobs_created,
            jobs_failed=jobs_failed,
            artifacts_registered=artifacts_registered,
            artifacts_failed=artifacts_failed,
            links_created=links_created,
            links_failed=links_failed,
            errors=errors,
        )

    def register_lineage_under_registration_session(
        self,
        registration_session_id: str,
        git_context: GitContext,
        jobs: list[dict],
        artifacts: list[dict] | None = None,
    ) -> BatchRegistrationResult:
        """Stage complete lineage under a remote registration session.

        Implements the 4-phase pattern for the bearer flow, matching the
        standard ``register_lineage``:

          Phase 2: create jobs under the registration session
          Phase 3: stage artifacts under the registration session
          Phase 4: link artifacts to jobs

        ``artifacts`` is accepted as optional for backwards-compatibility with
        callers that haven't been updated yet; when omitted, Phase 3 is
        skipped and the link endpoints' implicit stub-create remains the only
        path that brings artifact rows into existence (the original M1 bug).
        """
        errors: list[str] = []
        jobs_created = 0
        jobs_failed = 0
        artifacts_registered = 0
        artifacts_failed = 0
        links_created = 0
        links_failed = 0

        self._logger.debug(
            "Starting registration-session lineage staging: registration_session_id=%s, jobs=%d, artifacts=%d",
            registration_session_id,
            len(jobs),
            len(artifacts or []),
        )

        # Phase 2: Create jobs (no I/O links)
        job_uids_created: list[str] = []
        jobs_existing = 0
        if jobs:
            batch_results, batch_counts = (
                self.job_service.create_jobs_batch_under_registration_session(
                    jobs=jobs,
                    registration_session_id=registration_session_id,
                    git_context=git_context,
                )
            )
            for result in batch_results:
                if result.success:
                    # Successful jobs (newly-created OR already-staged) are all
                    # eligible for Phase 4 linking; the created/existing split
                    # comes from the server counts below.
                    job_uids_created.append(result.job_uid)
                else:
                    jobs_failed += 1
                    if result.error:
                        errors.append(f"Job {result.job_uid}: {result.error}")
            # Server reports how many of the staged jobs were newly created vs
            # already present under this registration session (a re-register).
            jobs_created = batch_counts.get("created", 0)
            jobs_existing = batch_counts.get("existing", 0)

            # Jobs this user already registered+finalized under other DAG(s).
            already_registered = [h for h in batch_counts.get("already_registered", []) if h]
            if already_registered:
                distinct = sorted(set(already_registered))
                staged_new = jobs_created + jobs_existing
                already_count = max(len(job_uids_created) - staged_new, 0)
                if staged_new == 0 and len(distinct) == 1:
                    # Full re-register: every job is already registered under one
                    # DAG. Skip Phase 3/4 + finalize; the caller reuses that DAG.
                    return BatchRegistrationResult(
                        session_registered=True,
                        jobs_created=0,
                        jobs_failed=0,
                        jobs_existing=0,
                        artifacts_registered=0,
                        artifacts_failed=0,
                        links_created=0,
                        links_failed=0,
                        errors=[],
                        already_registered_session_hash=distinct[0],
                        existing_binding_prepared=bool(
                            batch_counts.get("existing_binding_prepared", False)
                        ),
                    )
                # Partial overlap (some new + some already elsewhere) or jobs
                # spanning multiple DAGs — can't form one DAG without a job
                # belonging to two.
                dag_word = "DAG" if len(distinct) == 1 else "DAGs"
                job_word = "job" if already_count == 1 else "jobs"
                return BatchRegistrationResult(
                    session_registered=False,
                    jobs_created=jobs_created,
                    jobs_failed=max(already_count, 1),
                    jobs_existing=jobs_existing,
                    artifacts_registered=0,
                    artifacts_failed=0,
                    links_created=0,
                    links_failed=0,
                    errors=[
                        f"This lineage overlaps work already on GLaaS: {already_count} of its "
                        f"{job_word} are already registered under {len(distinct)} other {dag_word}. "
                        "roar can't yet register a lineage that extends or merges existing ones "
                        "(a job can belong to only one DAG)."
                    ],
                )

        self._logger.debug(
            "Registration-session job staging complete: %d created, %d failed",
            jobs_created,
            jobs_failed,
        )

        # Phase 3: Stage artifacts with real sizes (so Phase 4 link doesn't
        # have to fall back to size=0 stub-creation server-side).
        if artifacts:
            art_result = self.artifact_service.register_batch_under_registration_session(
                artifacts, registration_session_id
            )
            artifacts_registered += art_result.success_count
            artifacts_failed += art_result.error_count
            errors.extend(art_result.errors)
            self._logger.debug(
                "Registration-session artifact staging complete: %d staged, %d failed",
                art_result.success_count,
                art_result.error_count,
            )

        # Phase 4: Link artifacts to jobs
        for job in jobs:
            job_uid = job.get("job_uid")
            if not job_uid or job_uid not in job_uids_created:
                continue

            remote_job_uid = job.get("remote_job_uid")
            if not isinstance(remote_job_uid, str) or not remote_job_uid:
                remote_job_uid = job_uid

            inputs = self._extract_staged_io_list(job, "_inputs", "_input_hashes")
            outputs = self._extract_staged_io_list(job, "_outputs", "_output_hashes")
            view_edges = job.get("_view_edges")
            if not isinstance(view_edges, list):
                view_edges = []
            if not inputs and not outputs and not view_edges:
                continue

            link_result = self.job_service.link_job_artifacts_under_registration_session(
                registration_session_id=registration_session_id,
                job_uid=remote_job_uid,
                inputs=inputs,
                outputs=outputs,
                view_edges=view_edges,
            )
            if link_result.success:
                links_created += link_result.inputs_linked + link_result.outputs_linked
                artifacts_registered += link_result.artifacts_registered
            else:
                links_failed += 1
                artifacts_failed += link_result.artifacts_registered
                if link_result.error:
                    errors.append(f"Link {job_uid}: {link_result.error}")

        self._logger.debug(
            "Registration-session lineage staging complete: jobs=%d/%d, artifacts=%d, links=%d",
            jobs_created,
            jobs_created + jobs_failed,
            artifacts_registered,
            links_created,
        )

        return BatchRegistrationResult(
            session_registered=True,
            jobs_created=jobs_created,
            jobs_failed=jobs_failed,
            jobs_existing=jobs_existing,
            artifacts_registered=artifacts_registered,
            artifacts_failed=artifacts_failed,
            links_created=links_created,
            links_failed=links_failed,
            errors=errors,
        )

    def _resolve_io_artifact_hashes(
        self,
        items: list[dict[str, Any]],
        artifact_hash_cache: dict[str, str],
        artifact_kind: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve hash-based artifact refs to artifact-hash refs for link phase."""
        resolved_items: list[dict[str, Any]] = []
        errors: list[str] = []

        for item in items:
            ref_cache_key = _artifact_ref.cache_key(item)
            artifact_hash = artifact_hash_cache.get(ref_cache_key) if ref_cache_key else None

            if not artifact_hash:
                artifact_hash, lookup_error = self._resolve_artifact_hash(item)
                if lookup_error or not artifact_hash:
                    ref_preview = _artifact_ref.preview(item)
                    errors.append(
                        f"{artifact_kind} artifact {ref_preview or '<unknown>'} could not be resolved: {lookup_error or 'unknown error'}"
                    )
                    continue
                if ref_cache_key:
                    artifact_hash_cache[ref_cache_key] = artifact_hash

            resolved: dict[str, Any] = {
                "artifact_hash": artifact_hash,
                "path": item.get("path"),
            }
            if item.get("byte_ranges") is not None:
                resolved["byte_ranges"] = item["byte_ranges"]
            resolved_items.append(resolved)

        return resolved_items, errors

    def _resolve_artifact_hash(self, artifact_ref: dict[str, Any]) -> tuple[str | None, str | None]:
        """Resolve content hash for the given artifact reference."""
        return self.artifact_service.resolve_artifact_hash(artifact_ref)

    def _extract_io_list(
        self,
        job: dict,
        structured_key: str,
        hash_list_key: str,
    ) -> list[dict[str, Any]]:
        """
        Extract I/O list from job dict.

        Handles both structured format (_inputs/_outputs) and hash-only format
        (_input_hashes/_output_hashes).

        Args:
            job: Job dict
            structured_key: Key for structured I/O list (e.g., "_inputs")
            hash_list_key: Key for hash-only list (e.g., "_input_hashes")

        Returns:
            List of artifact refs with path, using either `hash` or `hashes`.
        """
        # Prefer structured format with path
        structured = job.get(structured_key, [])
        if structured:
            result: list[dict[str, Any]] = []
            for item in structured:
                h = item.get("hash")
                hashes = item.get("hashes")
                p = item.get("path")
                has_identity = bool(h) or bool(hashes)
                if has_identity and p:
                    item_dict: dict[str, Any] = {"path": p}
                    if h:
                        item_dict["hash"] = h
                    elif hashes:
                        item_dict["hashes"] = hashes
                    br = item.get("byte_ranges")
                    if br is not None:
                        item_dict["byte_ranges"] = br
                    result.append(item_dict)
                elif has_identity:
                    preview = h
                    if not preview and isinstance(hashes, list) and hashes:
                        preview = hashes[0].get("digest")
                    if preview:
                        self._logger.warning("Dropping I/O item %s: missing path", preview[:12])
                    else:
                        self._logger.warning("Dropping I/O item: missing path")
            return result

        # Fallback to hash-only format (path will be empty)
        hash_list = job.get(hash_list_key, [])
        if hash_list:
            return [{"hash": h, "path": ""} for h in hash_list if h]

        return []

    def _extract_staged_io_list(
        self,
        job: dict,
        structured_key: str,
        hash_list_key: str,
    ) -> list[dict[str, Any]]:
        """Extract staged I/O rows with direct artifact hashes for registration-session writes."""
        structured = job.get(structured_key, [])
        if structured:
            result: list[dict[str, Any]] = []
            for item in structured:
                artifact_hash = _artifact_ref.extract_digest(item)
                path = item.get("path")
                if not artifact_hash or not path:
                    preview = _artifact_ref.preview(item)
                    if preview:
                        self._logger.warning(
                            "Dropping staged I/O item %s: missing hash or path",
                            preview,
                        )
                    else:
                        self._logger.warning("Dropping staged I/O item: missing hash or path")
                    continue

                normalized: dict[str, Any] = {
                    "artifact_hash": artifact_hash,
                    "path": path,
                }
                if item.get("size") is not None:
                    normalized["size"] = item["size"]
                if item.get("source_type") is not None:
                    normalized["source_type"] = item["source_type"]
                if item.get("metadata") is not None:
                    normalized["metadata"] = item["metadata"]
                if item.get("byte_ranges") is not None:
                    normalized["byte_ranges"] = item["byte_ranges"]
                result.append(normalized)
            return result

        hash_list = job.get(hash_list_key, [])
        if hash_list:
            return [{"artifact_hash": h, "path": ""} for h in hash_list if h]

        return []

    def register_uploaded_artifacts(
        self,
        artifacts: list[tuple[str, int, str, str]],
        session_hash: str,
        scheme: str,
        dest_url: str,
        is_dir: bool,
    ) -> tuple[int, list[str]]:
        """
        Register uploaded artifacts (convenience method for put command).

        Args:
            artifacts: List of (hash, size, path, rel_path) tuples
            session_hash: Session hash
            scheme: Cloud storage scheme (s3, gs)
            dest_url: Destination URL
            is_dir: Whether destination is a directory

        Returns:
            Tuple of (registered_count, errors)
        """
        errors = []
        registered = 0

        for file_hash, size, _path, rel_path in artifacts:
            if is_dir:
                file_url = f"{dest_url.rstrip('/')}/{rel_path}"
            else:
                file_url = dest_url

            success, error = self.artifact_service.register_single(
                hashes=[{"algorithm": "blake3", "digest": file_hash}],
                size=size,
                source_type=scheme,
                session_hash=session_hash,
                source_url=file_url,
            )

            if success:
                registered += 1
            elif error:
                errors.append(f"{file_hash[:12]}: {error}")

        return registered, errors
