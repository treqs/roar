"""
File filter service for provenance collection.

Applies various filters to file lists based on configuration.
"""

import os
from pathlib import Path
from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import FilteredFiles, PythonInjectData, TracerData


class FileFilterService:
    """Filters files based on configuration settings."""

    # System paths to filter (for reads only)
    SYSTEM_READ_PREFIXES = (
        "/sys/",
        "/etc/",
        "/sbin/",
        "/proc/",
        "/dev/",
        "/usr/",  # All of /usr/ (share, local, lib, bin, etc.)
        "/opt/",
        "/lib/",
        "/lib64/",
    )

    # Torch/triton cache patterns
    TORCH_CACHE_PATTERNS = (
        "/tmp/torchinductor_",
        "/tmp/torch_",
        "/tmp/triton",
    )

    # Paths to always filter from written_files (not reproducibility-relevant)
    WRITE_NOISE_PREFIXES = (
        "/dev/",
        "/proc/",
        "/sys/",
        "/dev/shm/",
        "/usr/local/",  # System-managed tools (e.g., aws-cli writes cacert.pem here)
        "/usr/lib/",
        "/usr/share/",
        "/opt/",
    )

    def __init__(self, logger: ILogger | None = None) -> None:
        """
        Initialize file filter service.

        Args:
            logger: Logger for internal diagnostics
        """
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ....core.container import get_container
            from ....services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def filter_files(
        self,
        tracer_data: TracerData,
        python_data: PythonInjectData,
        config: dict[str, Any],
    ) -> FilteredFiles:
        """
        Apply filters to file lists.

        Args:
            tracer_data: Loaded tracer data
            python_data: Loaded Python inject data
            config: Configuration dict (from .roar.toml)

        Returns:
            FilteredFiles with filtered lists
        """
        self.logger.debug(
            "FileFilterService.filter_files: opened=%d, read=%d, written=%d",
            len(tracer_data.opened_files),
            len(tracer_data.read_files),
            len(tracer_data.written_files),
        )

        # Get filter settings
        filters_config = config.get("filters", {})
        ignore_system_reads = filters_config.get("ignore_system_reads", True)
        ignore_package_reads = filters_config.get("ignore_package_reads", True)
        ignore_torch_cache = filters_config.get("ignore_torch_cache", True)
        ignore_tmp_files = filters_config.get("ignore_tmp_files", True)
        self.logger.debug(
            "Filter config: system_reads=%s, package_reads=%s, torch_cache=%s, tmp_files=%s",
            ignore_system_reads,
            ignore_package_reads,
            ignore_torch_cache,
            ignore_tmp_files,
        )

        # Get cleanup settings
        cleanup_config = config.get("cleanup", {})
        delete_tmp_writes = cleanup_config.get("delete_tmp_writes", False)

        # Strict mode (delete_tmp_writes) overrides ignore_tmp_files
        if delete_tmp_writes:
            ignore_tmp_files = False

        # Create filter function for reads
        sys_prefix = python_data.sys_prefix
        sys_base_prefix = python_data.sys_base_prefix

        def should_include_read(path: str) -> bool:
            if ignore_system_reads and self._is_system_read(path):
                return False
            if ignore_torch_cache and self._is_torch_cache(path):
                return False
            if ignore_package_reads and self._is_package_file(path, sys_prefix, sys_base_prefix):
                return False
            return not (ignore_tmp_files and path.startswith("/tmp/"))

        # Apply filters to reads
        opened_files = [f for f in tracer_data.opened_files if should_include_read(f)]
        read_files = [f for f in tracer_data.read_files if should_include_read(f)]
        modules_files = [f for f in python_data.modules_files if should_include_read(f)]
        self.logger.debug(
            "After read filtering: opened=%d->%d, read=%d->%d, modules=%d->%d",
            len(tracer_data.opened_files),
            len(opened_files),
            len(tracer_data.read_files),
            len(read_files),
            len(python_data.modules_files),
            len(modules_files),
        )

        # Filter writes
        read_files_set: set[str] = set(tracer_data.read_files)
        tmp_files_to_delete: list[str] = []
        filtered_written_files: list[str] = []

        for f in tracer_data.written_files:
            # Skip noise (device files, proc, sys, etc.)
            if self._is_write_noise(f):
                continue
            # Skip torch cache from output (but don't delete - it's a persistent cache)
            if ignore_torch_cache and self._is_torch_cache(f):
                continue
            # Handle /tmp files
            if f.startswith("/tmp/"):
                if ignore_tmp_files:
                    # Skip /tmp files entirely (unless strict mode)
                    continue
                # Track /tmp files that were written (not read) for potential deletion
                if f not in read_files_set and delete_tmp_writes:
                    tmp_files_to_delete.append(f)
            filtered_written_files.append(f)

        # Delete /tmp files if strict mode enabled
        deleted_count = self._cleanup_tmp_files(tmp_files_to_delete)
        self.logger.debug(
            "After write filtering: written=%d->%d, tmp_deleted=%d",
            len(tracer_data.written_files),
            len(filtered_written_files),
            deleted_count,
        )

        return FilteredFiles(
            read_files=read_files,
            written_files=filtered_written_files,
            opened_files=opened_files,
            modules_files=modules_files,
            tmp_files_deleted=deleted_count,
        )

    def _is_system_read(self, path: str) -> bool:
        """Check if path is a system file read."""
        return path.startswith(self.SYSTEM_READ_PREFIXES)

    def _is_torch_cache(self, path: str) -> bool:
        """Check if path is a torch/triton cache file."""
        return any(path.startswith(pattern) for pattern in self.TORCH_CACHE_PATTERNS)

    def _is_package_file(self, path: str, sys_prefix: str, sys_base_prefix: str) -> bool:
        """Check if path is from an installed package."""
        # Check site-packages
        if "site-packages" in path:
            return True
        # Check if under sys_prefix (venv)
        if sys_prefix and path.startswith(str(Path(sys_prefix).resolve())):
            return True
        # Check stdlib (under sys_base_prefix but not site-packages)
        if sys_base_prefix:
            base = str(Path(sys_base_prefix).resolve())
            if path.startswith(base) and "site-packages" not in path:
                return True
        return False

    def _is_write_noise(self, path: str) -> bool:
        """Check if path is noise that shouldn't be in written_files."""
        if path.startswith(self.WRITE_NOISE_PREFIXES):
            return True
        # /etc writes are almost always noise (e.g., /etc/hosts lookup)
        if path.startswith("/etc/"):
            return True
        # roar's own files (log files in .roar/, etc.)
        if "/.roar/" in path or path.startswith(".roar/"):
            return True
        # Python bytecode cache files
        return bool(path.endswith(".pyc"))

    def _cleanup_tmp_files(self, files: list[str]) -> int:
        """
        Delete temporary files if strict mode enabled.

        Args:
            files: List of /tmp files to delete

        Returns:
            Number of files successfully deleted
        """
        if not files:
            return 0

        deleted_count = 0
        for tmp_file in files:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
                    deleted_count += 1
            except OSError:
                pass  # May not have permission, file in use, etc.

        if deleted_count > 0:
            self.logger.debug("Cleaned up %d /tmp file(s)", deleted_count)

        return deleted_count
