"""
No-op download backend for testing.

Simulates downloads without actually transferring data.
Useful for integration testing without cloud access.
"""

import os
from pathlib import Path

from .base import DownloadBackend


def should_skip_download() -> bool:
    """Check if downloads should be skipped (for testing)."""
    return os.environ.get("ROAR_GET_SKIP_DOWNLOAD", "").lower() in ("1", "true", "yes")


class NoOpDownloadBackend(DownloadBackend):
    """
    No-op download backend that simulates downloads.

    Creates files with predictable content without actual network access.
    Controlled by ROAR_GET_SKIP_DOWNLOAD environment variable.
    """

    def __init__(self, bucket: str, scheme: str = "s3"):
        """
        Initialize no-op download backend.

        Args:
            bucket: Simulated bucket name.
            scheme: URL scheme to use in URLs.
        """
        self._bucket = bucket
        self._scheme = scheme
        self._files: dict[str, bytes] = {}  # Simulated remote files

    def seed(self, key: str, content: bytes) -> None:
        """
        Seed a simulated remote file for testing.

        Args:
            key: Remote key.
            content: File content.
        """
        self._files[key] = content

    def download(self, remote_key: str, local_path: Path) -> None:
        """
        Simulate downloading a file.

        If the key was seeded, writes that content. Otherwise writes
        a placeholder.

        Args:
            remote_key: Key/path in the simulated storage.
            local_path: Local path to save the file to.

        Raises:
            FileNotFoundError: If key wasn't seeded and strict mode.
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if remote_key in self._files:
            local_path.write_bytes(self._files[remote_key])
        else:
            # Write placeholder content
            local_path.write_bytes(f"noop-content-for-{remote_key}".encode())

    def exists(self, remote_key: str) -> bool:
        """Check if a key was seeded."""
        return remote_key in self._files

    def list_keys(self, prefix: str) -> list[str]:
        """List all seeded keys matching prefix."""
        result = []
        list_prefix = prefix
        if list_prefix and not list_prefix.endswith("/"):
            list_prefix += "/"
        for key in self._files:
            if key.startswith(list_prefix) or key.startswith(prefix):
                result.append(key)
        return result
