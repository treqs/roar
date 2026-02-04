"""
In-memory storage backend for testing.

Stores files in memory instead of uploading to a real cloud provider.
"""

from pathlib import Path

from .base import StorageBackend


class MemoryBackend(StorageBackend):
    """
    In-memory storage backend for testing.

    Stores file contents in a dictionary, useful for unit tests
    that need to verify upload behavior without network access.
    """

    def __init__(self, bucket: str, prefix: str = ""):
        """
        Initialize memory backend.

        Args:
            bucket: Simulated bucket name.
            prefix: Simulated prefix/path.
        """
        self._bucket = bucket
        self._prefix = prefix
        self._storage: dict[str, bytes] = {}

    def upload(self, local_path: Path, remote_key: str) -> str:
        """
        Upload a file to memory storage.

        Args:
            local_path: Path to the local file.
            remote_key: Key/path in the simulated storage.

        Returns:
            Simulated URL of the uploaded file.
        """
        content = local_path.read_bytes()
        self._storage[remote_key] = content

        # Build the full URL
        if self._prefix:
            full_key = f"{self._prefix}/{remote_key}"
        else:
            full_key = remote_key

        return f"memory://{self._bucket}/{full_key}"

    def exists(self, remote_key: str) -> bool:
        """
        Check if a file exists in memory storage.

        Args:
            remote_key: Key/path in the simulated storage.

        Returns:
            True if the file exists, False otherwise.
        """
        return remote_key in self._storage

    def get_content(self, remote_key: str) -> bytes:
        """
        Get the content of a stored file.

        Args:
            remote_key: Key/path in the simulated storage.

        Returns:
            File content as bytes.

        Raises:
            KeyError: If the file doesn't exist.
        """
        return self._storage[remote_key]

    def list_keys(self) -> list[str]:
        """
        List all stored keys.

        Returns:
            List of all stored keys.
        """
        return list(self._storage.keys())
