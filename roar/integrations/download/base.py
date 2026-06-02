"""
Base download backend interface.

Defines the abstract interface that all download backends must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Supported source URI schemes
SUPPORTED_SOURCE_SCHEMES = {"s3", "gs", "http", "https", "hf"}


@dataclass
class Source:
    """Parsed source URL."""

    scheme: str
    bucket: str  # For cloud URLs; hostname for HTTP
    key: str  # Object key or URL path
    original_url: str
    _is_prefix: bool = False  # Set by parse_source based on trailing slash

    @property
    def is_prefix(self) -> bool:
        """Check if this source looks like a prefix/directory."""
        return self._is_prefix

    @property
    def filename(self) -> str:
        """Extract the filename from the key/path."""
        if self.is_prefix:
            return ""
        parts = self.key.rstrip("/").split("/")
        return parts[-1] if parts else ""


def parse_source(url: str) -> Source:
    """
    Parse a source URL into its components.

    Args:
        url: Source URL (e.g., s3://bucket/key, https://example.com/file.pt)

    Returns:
        Parsed Source object.

    Raises:
        ValueError: If the URL scheme is not supported.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in SUPPORTED_SOURCE_SCHEMES:
        raise ValueError(
            f"Unsupported source scheme: {scheme}. "
            f"Supported: {', '.join(sorted(SUPPORTED_SOURCE_SCHEMES))}"
        )

    if scheme == "hf":
        # hf://[datasets|models|spaces/]<owner>/<name>[@ref][/subpath]. The HF backend
        # re-parses original_url for repo@ref/subpath; here we only set bucket/key and
        # whether this is a whole-repo/dir (prefix) vs a single file.
        rest = url[len("hf://") :]
        body = rest.split("@", 1)[0]
        subpath = (
            "/".join(body.split("/")[3:])
            if body.startswith(("datasets/", "models/", "spaces/"))
            else "/".join(body.split("/")[2:])
        )
        last = subpath.rsplit("/", 1)[-1]
        is_prefix = subpath == "" or subpath.endswith("/") or "." not in last
        return Source(
            scheme="hf",
            bucket=body.rstrip("/"),
            key=subpath.rstrip("/"),
            original_url=url,
            _is_prefix=is_prefix,
        )
    elif scheme in ("s3", "gs"):
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        is_prefix = key.endswith("/") or key == ""
        key_stripped = key.rstrip("/")
        return Source(
            scheme=scheme,
            bucket=bucket,
            key=key_stripped,
            original_url=url,
            _is_prefix=is_prefix,
        )
    else:
        # HTTP/HTTPS
        return Source(
            scheme=scheme,
            bucket=parsed.netloc,
            key=parsed.path.lstrip("/"),
            original_url=url,
        )


class DownloadBackend(ABC):
    """
    Abstract base class for download backends.

    All download backends must implement download, exists, and list_keys methods.
    """

    @abstractmethod
    def download(self, remote_key: str, local_path: Path) -> None:
        """
        Download a file from the backend.

        Args:
            remote_key: Key/path in the remote storage.
            local_path: Local path to save the file to.

        Raises:
            FileNotFoundError: If the remote file doesn't exist.
            IOError: If download fails.
        """
        pass

    @abstractmethod
    def exists(self, remote_key: str) -> bool:
        """
        Check if a file exists in the backend.

        Args:
            remote_key: Key/path in the remote storage.

        Returns:
            True if the file exists, False otherwise.
        """
        pass

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """
        List all keys under a prefix.

        Args:
            prefix: Key prefix to list under.

        Returns:
            List of full keys under the prefix.
        """
        pass
