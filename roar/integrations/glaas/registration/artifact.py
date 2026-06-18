"""
Artifact registration service.

Consolidates artifact registration logic from put.py and coordinator.py.
"""

import json
from functools import cached_property
from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.registration import (
    ArtifactRegistrationResult,
    IArtifactRegistrar,
)
from ....core.logging import get_logger
from ....core.validation import validate_artifact_registration
from ..client import GlaasClient
from . import _artifact_ref

# Server body-parser limit is ~100KB, use 90KB for safety margin
MAX_BATCH_SIZE_BYTES = 90 * 1024  # 90KB


def _batch_by_size(
    artifacts: list[dict], max_bytes: int = MAX_BATCH_SIZE_BYTES
) -> list[list[dict]]:
    """Split artifacts into batches that fit within max_bytes when JSON-serialized.

    Args:
        artifacts: List of artifact dicts to batch
        max_bytes: Maximum JSON payload size per batch (default 90KB)

    Returns:
        List of batches, each fitting within max_bytes
    """
    if not artifacts:
        return []

    batches = []
    current_batch: list[dict] = []
    current_size = 2  # Account for "[]" wrapper

    for artifact in artifacts:
        # Calculate size of this artifact as JSON (including ", " separator)
        artifact_json = json.dumps(artifact)
        artifact_size = len(artifact_json) + 2  # +2 for ", " separator

        # If single artifact exceeds limit, send it alone
        if artifact_size > max_bytes:
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_size = 2
            batches.append([artifact])
            continue

        # If adding this artifact would exceed limit, start new batch
        if current_size + artifact_size > max_bytes:
            batches.append(current_batch)
            current_batch = [artifact]
            current_size = 2 + artifact_size
        else:
            current_batch.append(artifact)
            current_size += artifact_size

    if current_batch:
        batches.append(current_batch)

    return batches


class ArtifactRegistrationService(IArtifactRegistrar):
    """
    Service for artifact registration operations.

    Consolidates the duplicated artifact registration logic from:
    - put.py:391-468 (uploaded and lineage artifacts)
    - RegisterService (registration via roar register command)
    """

    def __init__(self, client: GlaasClient | None = None, logger: ILogger | None = None):
        """
        Initialize the artifact registration service.

        Args:
            client: GLaaS client for server communication. If None, creates one.
            logger: Logger instance. If None, uses the configured process logger.
        """
        self._client = client

        self._logger = logger or get_logger()

    @cached_property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        return self._client or GlaasClient()

    def register_single(
        self,
        hashes: list[dict[str, str]],
        size: int,
        source_type: str | None,
        session_hash: str,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Register a single artifact with validation.

        This consolidates the duplicated single artifact registration from:
        - put.py:406-419
        - put.py:455-465

        Args:
            hashes: List of {algorithm, digest} dicts
            size: File size in bytes
            source_type: Source type ('s3', 'gs', 'https', or None)
            session_hash: Session this artifact belongs to
            source_url: Optional source URL
            metadata: Optional JSON metadata

        Returns:
            Tuple of (success, error_message)
        """
        # Validate artifact data
        validation = validate_artifact_registration(
            hashes=hashes,
            size=size,
            source_type=source_type,
            session_hash=session_hash,
        )
        if not validation:
            error_msg = "; ".join(validation.errors)
            self._logger.warning(
                "Artifact validation failed: %s (hash=%s)",
                error_msg,
                hashes[0].get("digest", "")[:12] if hashes else "none",
            )
            return False, error_msg

        # Register with GLaaS
        success, error = self.client.register_artifact(
            hashes=hashes,
            size=size,
            source_type=source_type or "local",
            session_hash=session_hash,
            source_url=source_url,
            metadata=metadata,
        )

        if error:
            self._logger.debug(
                "Artifact registration failed: hash=%s, error=%s",
                hashes[0].get("digest", "")[:12] if hashes else "none",
                error,
            )
        else:
            self._logger.debug(
                "Artifact registered: hash=%s",
                hashes[0].get("digest", "")[:12] if hashes else "none",
            )

        return success, error

    def register_batch(
        self,
        artifacts: list[dict],
        session_hash: str,
    ) -> ArtifactRegistrationResult:
        """
        Register multiple artifacts in batch with validation.

        Each artifact dict should contain:
        - hashes: List of {algorithm, digest} dicts
        - size: File size in bytes
        - source_type: Source type (optional, defaults to None)
        - source_url: Source URL (optional)
        - metadata: JSON metadata (optional)

        Args:
            artifacts: List of artifact dicts to register
            session_hash: Session these artifacts belong to

        Returns:
            ArtifactRegistrationResult with counts and errors
        """
        if not artifacts:
            return ArtifactRegistrationResult(
                success_count=0,
                error_count=0,
                errors=[],
            )

        self._logger.debug("Preparing %d artifacts for batch registration", len(artifacts))

        # Validate and prepare artifacts
        valid_artifacts = []
        errors = []

        for i, art in enumerate(artifacts):
            hashes = art.get("hashes", [])
            size = art.get("size")
            source_type = art.get("source_type")

            # Handle single hash format
            if not hashes and art.get("hash"):
                hashes = [{"algorithm": "blake3", "digest": art["hash"]}]

            validation = validate_artifact_registration(
                hashes=hashes,
                size=size,
                source_type=source_type,
                session_hash=session_hash,
            )

            if not validation:
                hash_preview = hashes[0].get("digest", "")[:12] if hashes else "none"
                error_msg = f"Artifact {i} ({hash_preview}): {'; '.join(validation.errors)}"
                self._logger.warning("Skipping invalid artifact: %s", error_msg)
                errors.append(error_msg)
                continue

            # Build artifact payload
            payload = {
                "hashes": hashes,
                "size": size,
                "source_type": source_type,
                "session_hash": session_hash,
            }
            if art.get("source_url"):
                payload["source_url"] = art["source_url"]
            if art.get("metadata"):
                payload["metadata"] = art["metadata"]

            valid_artifacts.append(payload)

        if not valid_artifacts:
            return ArtifactRegistrationResult(
                success_count=0,
                error_count=len(errors),
                errors=errors,
            )

        # Register batches with GLaaS using size-based batching to avoid exceeding
        # server body-parser limits (~100KB)
        total_success = 0
        total_errors = 0
        # Distinct *artifacts* registered (one per batch entry), as opposed to the
        # server's per-content-hash tally — an artifact with both a blake3 and a
        # sha256 counts once. This is what we report, so the count matches
        # `register --dry-run` (len(lineage.artifacts)) instead of doubling.
        distinct_registered = 0

        batches = _batch_by_size(valid_artifacts)
        total_batches = len(batches)
        self._logger.debug(
            "Split %d valid artifacts into %d batches for registration",
            len(valid_artifacts),
            total_batches,
        )

        for batch_idx, batch in enumerate(batches):
            batch_size_bytes = len(json.dumps(batch))
            self._logger.debug(
                "Sending batch %d/%d: %d artifacts (%d bytes)",
                batch_idx + 1,
                total_batches,
                len(batch),
                batch_size_bytes,
            )
            success_count, error_count, batch_error = self.client.register_artifacts_batch(batch)

            total_success += success_count
            total_errors += error_count

            if batch_error:
                errors.append(f"Batch registration error: {batch_error}")
                self._logger.warning("Batch artifact registration failed: %s", batch_error)
                break  # Stop on first batch error
            distinct_registered += len(batch)
            self._logger.debug(
                "Batch artifact registration: %d success, %d errors (batch of %d)",
                success_count,
                error_count,
                len(batch),
            )

        return ArtifactRegistrationResult(
            success_count=distinct_registered,
            error_count=total_errors + len(errors),
            errors=errors,
        )

    def register_batch_under_registration_session(
        self,
        artifacts: list[dict],
        registration_session_id: str,
    ) -> ArtifactRegistrationResult:
        """Stage a batch of artifacts under a registration session (Phase 3 bearer flow).

        Mirrors ``register_batch`` but does not include ``session_hash`` on
        each artifact (the registration_session_id in the URL path scopes the
        request server-side).
        """
        if not artifacts:
            return ArtifactRegistrationResult(success_count=0, error_count=0, errors=[])

        self._logger.debug(
            "Preparing %d artifacts for staged registration in registration session %s",
            len(artifacts),
            registration_session_id,
        )

        valid_artifacts: list[dict] = []
        errors: list[str] = []

        for i, art in enumerate(artifacts):
            hashes = art.get("hashes", [])
            size = art.get("size")
            source_type = art.get("source_type")

            if not hashes and art.get("hash"):
                hashes = [{"algorithm": "blake3", "digest": art["hash"]}]

            # Reuse the standard validator. The registration_session_id is a
            # real identifier, not a placeholder, so it satisfies the
            # non-placeholder check; we strip session_hash from the payload
            # below since the staged endpoint scopes by URL.
            validation = validate_artifact_registration(
                hashes=hashes,
                size=size,
                source_type=source_type,
                session_hash=registration_session_id,
            )

            if not validation:
                hash_preview = hashes[0].get("digest", "")[:12] if hashes else "none"
                error_msg = f"Artifact {i} ({hash_preview}): {'; '.join(validation.errors)}"
                self._logger.warning("Skipping invalid staged artifact: %s", error_msg)
                errors.append(error_msg)
                continue

            payload: dict = {
                "hashes": hashes,
                "size": size,
                "source_type": source_type,
            }
            if art.get("source_url"):
                payload["source_url"] = art["source_url"]
            if art.get("metadata"):
                payload["metadata"] = art["metadata"]

            valid_artifacts.append(payload)

        if not valid_artifacts:
            return ArtifactRegistrationResult(
                success_count=0,
                error_count=len(errors),
                errors=errors,
            )

        total_success = 0
        total_errors = 0
        # Distinct artifacts (one per batch entry), not the server's per-hash
        # tally — see register_batch. Reported so the count matches the dry-run.
        distinct_registered = 0

        batches = _batch_by_size(valid_artifacts)
        total_batches = len(batches)
        self._logger.debug(
            "Split %d valid staged artifacts into %d batches for registration",
            len(valid_artifacts),
            total_batches,
        )

        for batch_idx, batch in enumerate(batches):
            batch_size_bytes = len(json.dumps(batch))
            self._logger.debug(
                "Sending staged batch %d/%d: %d artifacts (%d bytes)",
                batch_idx + 1,
                total_batches,
                len(batch),
                batch_size_bytes,
            )
            success_count, error_count, batch_error = (
                self.client.register_artifacts_batch_under_registration_session(
                    registration_session_id, batch
                )
            )

            # Backwards compat with glaas instances that pre-date the staged
            # artifact endpoint (https://github.com/treqs-inc/glaas-api/pull/50).
            # 404 on the very first batch means the server doesn't know the
            # endpoint; bail out cleanly so the bearer link path's
            # implicit stub-create still works as the legacy fallback. (This is
            # exactly the pre-Phase-3 behavior — has the M1 bug, but doesn't
            # break the register itself.)
            if batch_error and "HTTP 404" in batch_error and batch_idx == 0 and total_success == 0:
                self._logger.info(
                    "Phase 3 endpoint not present on this glaas instance (HTTP 404); "
                    "falling back to legacy link-implicit artifact creation. "
                    "Upgrade glaas-api to fix the 0-byte artifact issue."
                )
                return ArtifactRegistrationResult(
                    success_count=0,
                    error_count=0,
                    errors=[],
                )

            total_success += success_count
            total_errors += error_count

            if batch_error:
                errors.append(f"Staged batch registration error: {batch_error}")
                self._logger.warning("Staged batch artifact registration failed: %s", batch_error)
                break
            distinct_registered += len(batch)

        return ArtifactRegistrationResult(
            success_count=distinct_registered,
            error_count=total_errors + len(errors),
            errors=errors,
        )

    def resolve_artifact_hash(self, artifact_ref: dict[str, Any]) -> tuple[str | None, str | None]:
        """Resolve a server artifact hash from a hash/hash-list artifact reference.

        Returns the canonical content hash (not the server UUID) because the
        job-linking endpoints (``/jobs/:uid/inputs``, ``/jobs/:uid/outputs``)
        identify artifacts by their content hash.
        """
        digest = _artifact_ref.extract_digest(artifact_ref)
        if not digest:
            return None, "missing hash/hash list"

        try:
            artifact = self.client.get_artifact(digest)
        except Exception as exc:  # pragma: no cover - exercised via coordinator tests with mocks
            return None, str(exc)

        artifact_hash = artifact.get("hash") if isinstance(artifact, dict) else None
        if not isinstance(artifact_hash, str) or not artifact_hash:
            return None, f"artifact lookup returned no hash for digest {digest}"
        return artifact_hash, None

    def build_artifact_payload(
        self,
        file_hash: str,
        size: int,
        source_type: str | None,
        session_hash: str,
        source_url: str | None = None,
    ) -> dict:
        """
        Build artifact payload dict for batch registration.

        Convenience method to create properly formatted artifact dicts.

        Args:
            file_hash: Blake3 hash of the file
            size: File size in bytes
            source_type: Source type ('s3', 'gs', etc.)
            session_hash: Session hash
            source_url: Optional source URL

        Returns:
            Dict ready for register_batch()
        """
        payload = {
            "hashes": [{"algorithm": "blake3", "digest": file_hash}],
            "size": size,
            "source_type": source_type,
            "session_hash": session_hash,
        }
        if source_url:
            payload["source_url"] = source_url
        return payload
