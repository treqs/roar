"""
No-op storage backend for testing.

Simulates uploads without actually transferring data.
Useful for integration testing GLaaS registration without cloud access.
"""

import os
from pathlib import Path

from .base import StorageBackend


def should_skip_upload() -> bool:
    """Check if uploads should be skipped (for testing)."""
    return os.environ.get("ROAR_PUT_SKIP_UPLOAD", "").lower() in ("1", "true", "yes")


class NoOpBackend(StorageBackend):
    """
    No-op storage backend that simulates uploads.

    Returns valid-looking URLs without actually uploading data.
    Controlled by ROAR_PUT_SKIP_UPLOAD environment variable.
    """

    def __init__(self, bucket: str, prefix: str = "", scheme: str = "s3"):
        """
        Initialize no-op backend.

        Args:
            bucket: Simulated bucket name.
            prefix: Simulated prefix/path.
            scheme: URL scheme to use in returned URLs.
        """
        self._bucket = bucket
        self._prefix = prefix
        self._scheme = scheme
        self._uploaded: dict[str, int] = {}  # key -> size

    def upload(self, local_path: Path, remote_key: str) -> str:
        """
        Simulate uploading a file.

        Args:
            local_path: Path to the local file.
            remote_key: Key/path in the simulated storage.

        Returns:
            Simulated URL of the "uploaded" file.
        """
        # Record the upload (for verification)
        self._uploaded[remote_key] = local_path.stat().st_size

        # Build the full URL
        if self._prefix:
            full_key = f"{self._prefix}/{remote_key}"
        else:
            full_key = remote_key

        return f"{self._scheme}://{self._bucket}/{full_key}"

    def exists(self, remote_key: str) -> bool:
        """
        Check if a file was "uploaded".

        Args:
            remote_key: Key/path in the simulated storage.

        Returns:
            True if upload() was called for this key.
        """
        return remote_key in self._uploaded

    def get_uploaded_keys(self) -> list[str]:
        """Get list of all "uploaded" keys."""
        return list(self._uploaded.keys())

    def get_uploaded_size(self, remote_key: str) -> int | None:
        """Get the size of an "uploaded" file."""
        return self._uploaded.get(remote_key)
