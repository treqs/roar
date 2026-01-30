"""
Artifact registration service.

Consolidates artifact registration logic from put.py and coordinator.py.
"""

import json

from ...core.di import resolve_or_default
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import (
    ArtifactRegistrationResult,
    IArtifactRegistrar,
)
from ...core.validation import validate_artifact_registration
from ...glaas_client import GlaasClient

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
            logger: Logger instance. If None, resolves from DI container.
        """
        self._client = client
        from ...services.logging import NullLogger

        self._logger = logger or resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]

    @property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        if self._client is None:
            self._client = GlaasClient()
        return self._client

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
            else:
                self._logger.debug(
                    "Batch artifact registration: %d success, %d errors (batch of %d)",
                    success_count,
                    error_count,
                    len(batch),
                )

        return ArtifactRegistrationResult(
            success_count=total_success,
            error_count=total_errors + len(errors),
            errors=errors,
        )

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
