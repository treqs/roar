"""Publish-specific storage backend resolution."""

from __future__ import annotations

from ..resolution import load_backend_class, resolve_backend_for_scheme
from .base import StorageBackend, parse_destination
from .memory import MemoryBackend
from .noop import NoOpBackend, should_skip_upload


def resolve_publish_storage_backend(destination: str) -> StorageBackend:
    """Resolve the storage backend used by `roar put`."""
    parsed = parse_destination(destination)

    if should_skip_upload():
        return NoOpBackend(
            bucket=parsed.bucket,
            prefix=parsed.prefix,
            scheme=parsed.scheme,
        )

    builders = {
        "s3": lambda: load_backend_class(
            "roar.integrations.storage.s3",
            "S3Backend",
            "S3 backend requires boto3. Install with: pip install boto3",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "gs": lambda: load_backend_class(
            "roar.integrations.storage.gcs",
            "GCSBackend",
            "GCS backend requires google-cloud-storage. Install with: pip install google-cloud-storage",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "memory": lambda: MemoryBackend(bucket=parsed.bucket, prefix=parsed.prefix),
    }

    return resolve_backend_for_scheme(
        parsed.scheme,
        builders,
        f"Unsupported destination scheme: {parsed.scheme}",
    )
