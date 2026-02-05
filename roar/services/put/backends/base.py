"""
Base storage backend interface.

Defines the abstract interface that all storage backends must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Supported URI schemes
SUPPORTED_SCHEMES = {"s3", "gs", "hf", "wandb", "memory"}


@dataclass
class Destination:
    """Parsed destination URL."""

    scheme: str
    bucket: str
    prefix: str

    @property
    def full_prefix(self) -> str:
        """Return the full prefix path."""
        return self.prefix


def parse_destination(url: str) -> Destination:
    """
    Parse a destination URL into its components.

    Args:
        url: Destination URL (e.g., s3://bucket/prefix)

    Returns:
        Parsed Destination object.

    Raises:
        ValueError: If the URL scheme is not supported.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(
            f"Unsupported destination scheme: {scheme}. "
            f"Supported: {', '.join(sorted(SUPPORTED_SCHEMES))}"
        )

    bucket = parsed.netloc
    # Strip leading and trailing slashes from path
    prefix = parsed.path.strip("/")

    return Destination(scheme=scheme, bucket=bucket, prefix=prefix)


class StorageBackend(ABC):
    """
    Abstract base class for storage backends.

    All storage backends must implement upload and exists methods.
    """

    @abstractmethod
    def upload(self, local_path: Path, remote_key: str) -> str:
        """
        Upload a file to the storage backend.

        Args:
            local_path: Path to the local file.
            remote_key: Key/path in the remote storage.

        Returns:
            Full URL of the uploaded file.
        """
        pass

    @abstractmethod
    def exists(self, remote_key: str) -> bool:
        """
        Check if a file exists in the storage backend.

        Args:
            remote_key: Key/path in the remote storage.

        Returns:
            True if the file exists, False otherwise.
        """
        pass
