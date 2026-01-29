"""
Cloud storage provider interface definitions.

Enables pluggable cloud storage backends (S3, GCS, Azure, etc.)
following the Open/Closed Principle.
"""

from abc import ABC, abstractmethod


class ICloudStorageProvider(ABC):
    """
    Interface for cloud storage providers.

    Implementations must handle their specific cloud SDK interactions
    while conforming to this common interface.
    """

    @property
    @abstractmethod
    def scheme(self) -> str:
        """
        Return the URL scheme this provider handles.

        Examples: 's3', 'gs', 'az', 'minio'
        """
        pass

    @property
    @abstractmethod
    def cli_tool(self) -> str:
        """Return the CLI tool name for this provider."""
        pass

    @property
    @abstractmethod
    def install_hint(self) -> str:
        """Return installation hint for the CLI tool."""
        pass

    @abstractmethod
    def check_cli_available(self) -> tuple[bool, str]:
        """
        Check if the required CLI tool is available.

        Returns:
            (available, tool_name)
        """
        pass

    @abstractmethod
    def parse_url(self, url: str) -> tuple[str, str]:
        """
        Parse a cloud URL into (bucket, key).

        Args:
            url: Cloud URL (e.g., s3://bucket/key)

        Returns:
            (bucket, key) tuple
        """
        pass

    @abstractmethod
    def download(
        self,
        source_url: str,
        dest_path: str,
        recursive: bool = False,
    ) -> tuple[bool, str]:
        """
        Download from cloud storage.

        Args:
            source_url: Cloud URL
            dest_path: Local destination path
            recursive: If True, download directory recursively

        Returns:
            (success, error_message)
        """
        pass

    @abstractmethod
    def upload(
        self,
        source_path: str,
        dest_url: str,
        recursive: bool = False,
        show_progress: bool = True,
    ) -> tuple[bool, str]:
        """
        Upload to cloud storage.

        Args:
            source_path: Local source path
            dest_url: Cloud URL
            recursive: If True, upload directory recursively
            show_progress: If True, show upload progress

        Returns:
            (success, error_message)
        """
        pass

    @abstractmethod
    def list_objects(self, url: str) -> tuple[bool, list[str], str]:
        """
        List objects at a cloud URL.

        Args:
            url: Cloud URL prefix

        Returns:
            (success, file_list, error_message)
        """
        pass

    @abstractmethod
    def upload_batch(
        self,
        files: list[tuple[str, str]],
        show_progress: bool = True,
    ) -> tuple[bool, str]:
        """
        Upload multiple files with progress tracking.

        Args:
            files: List of (local_path, dest_url) tuples
            show_progress: Whether to show progress bar

        Returns:
            (success, error_message)
        """
        pass
