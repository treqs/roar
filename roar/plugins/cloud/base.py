"""
Base cloud storage provider.

Defines the interface and common functionality for cloud storage providers.
"""

import subprocess
import sys
import threading
from abc import abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ...core.interfaces.cloud import ICloudStorageProvider


@dataclass
class CloudFile:
    """Represents a file in cloud storage."""

    bucket: str
    key: str
    size: int | None = None
    etag: str | None = None

    @property
    def url(self) -> str:
        """Get the full URL for this file."""
        raise NotImplementedError("Subclasses must implement url property")


@dataclass
class UploadProgress:
    """Tracks upload progress across multiple files."""

    total_bytes: int
    file_count: int
    bytes_transferred: int = 0
    files_completed: int = 0
    current_file: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _start_time: float | None = field(default=None, repr=False)
    _bar_width: int = field(default=40, repr=False)
    _last_displayed_eta: float | None = field(default=None, repr=False)
    _last_render_time: float = field(default=0, repr=False)

    def start(self):
        """Start tracking progress."""
        import time

        self._start_time = time.time()

    def set_current_file(self, filename: str):
        """Set the currently uploading file."""
        with self._lock:
            self.current_file = filename

    def file_completed(self):
        """Mark current file as completed."""
        with self._lock:
            self.files_completed += 1

    def add_bytes(self, bytes_amount: int):
        """Add bytes to the transfer count (incremental)."""
        import time

        with self._lock:
            self.bytes_transferred += bytes_amount
            now = time.time()
            if now - self._last_render_time >= 0.1:
                self._render()
                self._last_render_time = now

    def set_bytes(self, bytes_transferred: int):
        """Set absolute bytes transferred."""
        import time

        with self._lock:
            self.bytes_transferred = bytes_transferred
            now = time.time()
            if now - self._last_render_time >= 0.1:
                self._render()
                self._last_render_time = now

    def _render(self):
        """Render progress bar to stdout."""
        import time

        if self._start_time is None:
            return

        now = time.time()
        elapsed = now - self._start_time
        if elapsed < 0.5:
            return

        pct = self.bytes_transferred / self.total_bytes if self.total_bytes > 0 else 0
        filled = int(self._bar_width * pct)
        bar = "█" * filled + "░" * (self._bar_width - filled)

        avg_speed = self.bytes_transferred / elapsed
        remaining_bytes = self.total_bytes - self.bytes_transferred
        raw_eta = remaining_bytes / avg_speed if avg_speed > 0 else 0

        if (
            self._last_displayed_eta is None
            or raw_eta <= self._last_displayed_eta
            or raw_eta > self._last_displayed_eta * 1.1
        ):
            display_eta = raw_eta
        else:
            display_eta = self._last_displayed_eta

        self._last_displayed_eta = display_eta

        filename = self.current_file
        if len(filename) > 20:
            filename = "..." + filename[-17:]

        status = (
            f"\r  [{bar}] {pct * 100:5.1f}% "
            f"{_format_size(self.bytes_transferred)}/{_format_size(self.total_bytes)} "
            f"{_format_speed(avg_speed)} ETA {_format_duration(display_eta)} "
            f"({self.files_completed}/{self.file_count}) {filename}"
        )
        sys.stdout.write(status.ljust(120)[:120])
        sys.stdout.flush()

    def finish(self):
        """Finalize progress display."""
        import time

        elapsed = time.time() - self._start_time if self._start_time else 0
        speed = self.bytes_transferred / elapsed if elapsed > 0 else 0
        print(
            f"\r  Uploaded {_format_size(self.total_bytes)} in {_format_duration(elapsed)} ({_format_speed(speed)})"
            + " " * 40
        )


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable string."""
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


def _format_speed(bytes_per_sec: float) -> str:
    """Format speed in bytes/sec to human readable string."""
    return f"{_format_size(int(bytes_per_sec))}/s"


def _format_duration(seconds: float) -> str:
    """Format duration in seconds to human readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}:{m:02d}:{s:02d}"


class BaseCloudProvider(ICloudStorageProvider):
    """
    Abstract base class for cloud storage providers.

    Implements the Strategy pattern for cloud operations.
    Inherits from ICloudStorageProvider interface for DI container integration.
    """

    @property
    @abstractmethod
    def scheme(self) -> str:
        """Return the URL scheme this provider handles (e.g., 's3', 'gs')."""
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

    def check_cli_available(self) -> tuple[bool, str]:
        """
        Check if the required CLI tool is available.

        Returns:
            (available, tool_name)
        """
        try:
            subprocess.run(self._cli_version_command(), capture_output=True, check=True)
            return True, self.cli_tool
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False, self.cli_tool

    @abstractmethod
    def _cli_version_command(self) -> list[str]:
        """Return the command to check CLI version."""
        pass

    def parse_url(self, url: str) -> tuple[str, str]:
        """
        Parse a cloud URL into (bucket, key).

        Args:
            url: Cloud URL (e.g., s3://bucket/key)

        Returns:
            (bucket, key) tuple
        """
        parsed = urlparse(url)
        if parsed.scheme.lower() != self.scheme:
            raise ValueError(f"URL scheme must be {self.scheme}://, got {parsed.scheme}://")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        return bucket, key

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
