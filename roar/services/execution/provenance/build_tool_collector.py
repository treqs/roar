"""
Build tool collector service for provenance collection.

Detects build tools (cmake, gcc, ninja, etc.) invoked during execution
by analyzing the process tree captured by the tracer.
"""

import os
import shutil
import subprocess
from typing import Any

from ....core.interfaces.logger import ILogger

KNOWN_BUILD_TOOLS: frozenset[str] = frozenset(
    {
        "cmake",
        "gcc",
        "g++",
        "cc",
        "c++",
        "make",
        "gmake",
        "ninja",
        "meson",
        "rustc",
        "cargo",
        "nvcc",
        "ar",
        "ld",
        "as",
        "ranlib",
        "strip",
        "pkg-config",
        "autoconf",
        "automake",
        "libtool",
        "nasm",
    }
)


class BuildToolCollectorService:
    """Collects build tool dependencies from the traced process tree."""

    def __init__(self, logger: ILogger | None = None) -> None:
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

    def collect(
        self,
        processes: list[dict[str, Any]],
        sys_prefix: str,
    ) -> dict[str, str]:
        """
        Analyze traced processes to find build tool dependencies.

        Args:
            processes: Process list from TracerData.processes
            sys_prefix: Python sys.prefix to exclude pip-installed tools

        Returns:
            Dict of dpkg package names to versions for build tools
        """
        if not processes:
            return {}

        # Extract unique build tool basenames from process commands
        tool_basenames: set[str] = set()
        for proc in processes:
            command = proc.get("command", [])
            if not command:
                continue
            basename = os.path.basename(command[0])
            if basename in KNOWN_BUILD_TOOLS:
                tool_basenames.add(basename)

        if not tool_basenames:
            self.logger.debug("No build tools found in process tree")
            return {}

        self.logger.debug("Found build tools in process tree: %s", tool_basenames)

        # Resolve full paths and filter out pip-installed tools
        paths_to_resolve: list[str] = []
        for tool in tool_basenames:
            full_path = shutil.which(tool)
            if not full_path:
                self.logger.debug("Build tool %s not found on PATH, skipping", tool)
                continue
            if self._is_under_prefix(full_path, sys_prefix):
                self.logger.debug("Build tool %s is under sys_prefix, skipping", tool)
                continue
            if "site-packages" in full_path:
                self.logger.debug("Build tool %s is in site-packages, skipping", tool)
                continue
            paths_to_resolve.append(full_path)

        if not paths_to_resolve:
            self.logger.debug("No system build tool paths to resolve")
            return {}

        # Batch dpkg -S to map paths to packages
        pkg_names = self._resolve_dpkg_packages(paths_to_resolve)
        if not pkg_names:
            return {}

        # Query versions
        return self._query_dpkg_versions(pkg_names)

    def _is_under_prefix(self, path: str, sys_prefix: str) -> bool:
        """Check if path is under sys_prefix."""
        if not sys_prefix:
            return False
        try:
            os.path.relpath(path, sys_prefix)
            return path.startswith(sys_prefix)
        except ValueError:
            return False

    def _resolve_dpkg_packages(self, paths: list[str]) -> set[str]:
        """Batch resolve file paths to dpkg package names."""
        pkg_names: set[str] = set()
        try:
            result = subprocess.run(
                ["dpkg", "-S", *paths],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # dpkg -S outputs "package: /path/to/file" per line
            # It may return non-zero if some paths aren't owned, but still
            # outputs matches on stdout
            for line in result.stdout.strip().split("\n"):
                if ": " in line:
                    pkg_part = line.split(": ", 1)[0]
                    # Handle arch-qualified names like "cmake:amd64"
                    pkg_name = pkg_part.split(":")[0].strip()
                    if pkg_name:
                        pkg_names.add(pkg_name)
        except Exception as e:
            self.logger.debug("dpkg -S failed for build tools: %s", e)

        self.logger.debug("Resolved %d dpkg packages from build tools", len(pkg_names))
        return pkg_names

    def _query_dpkg_versions(self, pkg_names: set[str]) -> dict[str, str]:
        """Query dpkg for package versions."""
        result_packages: dict[str, str] = {}
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n", *sorted(pkg_names)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if "\t" in line:
                        pkg, version = line.split("\t", 1)
                        result_packages[pkg] = version
        except Exception as e:
            self.logger.debug("dpkg-query failed for build tools: %s", e)
            # Fall back to empty versions
            for pkg in pkg_names:
                result_packages[pkg] = ""

        self.logger.debug("Build tool dpkg packages: %s", result_packages)
        return result_packages
