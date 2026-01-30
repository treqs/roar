"""
Build pip collector service for provenance collection.

Detects Python build tools (uv, pip, maturin, etc.) invoked during execution
by analyzing the process tree captured by the tracer. Unlike build_tool_collector
which finds system (dpkg) packages, this finds pip-installed Python tools.
"""

import os
import shutil
import subprocess
from typing import Any

from ....core.interfaces.logger import ILogger

KNOWN_PYTHON_BUILD_TOOLS: frozenset[str] = frozenset(
    {
        "uv",
        "pip",
        "pip3",
        "setuptools",
        "maturin",
        "hatch",
        "flit",
        "poetry",
        "pdm",
        "pipx",
    }
)


class BuildPipCollectorService:
    """Collects pip-installed Python build tool dependencies from the traced process tree."""

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
        Analyze traced processes to find pip-installed Python build tools.

        Args:
            processes: Process list from TracerData.processes
            sys_prefix: Python sys.prefix to keep only pip-installed tools

        Returns:
            Dict of pip package names to versions for build tools
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
            if basename in KNOWN_PYTHON_BUILD_TOOLS:
                tool_basenames.add(basename)

        if not tool_basenames:
            self.logger.debug("No Python build tools found in process tree")
            return {}

        self.logger.debug("Found Python build tools in process tree: %s", tool_basenames)

        # Resolve full paths and keep only pip-installed tools (under sys_prefix or site-packages)
        tools_to_query: set[str] = set()
        for tool in tool_basenames:
            full_path = shutil.which(tool)
            if not full_path:
                self.logger.debug("Python build tool %s not found on PATH, skipping", tool)
                continue
            if self._is_under_prefix(full_path, sys_prefix) or "site-packages" in full_path:
                tools_to_query.add(tool)
                self.logger.debug("Python build tool %s is pip-installed, keeping", tool)
            else:
                self.logger.debug("Python build tool %s is not pip-installed, skipping", tool)

        if not tools_to_query:
            self.logger.debug("No pip-installed Python build tools to query")
            return {}

        # Query versions via importlib.metadata in a subprocess
        return self._query_pip_versions(tools_to_query)

    def _is_under_prefix(self, path: str, sys_prefix: str) -> bool:
        """Check if path is under sys_prefix."""
        if not sys_prefix:
            return False
        try:
            os.path.relpath(path, sys_prefix)
            return path.startswith(sys_prefix)
        except ValueError:
            return False

    def _query_pip_versions(self, tool_names: set[str]) -> dict[str, str]:
        """Query pip package versions for the given tool names."""
        result_packages: dict[str, str] = {}

        for tool in sorted(tool_names):
            # Normalize: pip3 -> pip
            pkg_name = "pip" if tool == "pip3" else tool
            version = self._get_package_version(pkg_name)
            if version is not None:
                result_packages[pkg_name] = version
            else:
                self.logger.debug("Could not determine version for %s", pkg_name)

        self.logger.debug("Build pip packages: %s", result_packages)
        return result_packages

    def _get_package_version(self, package_name: str) -> str | None:
        """Get version of a pip package using importlib.metadata."""
        try:
            result = subprocess.run(
                [
                    "python",
                    "-c",
                    f"import importlib.metadata; print(importlib.metadata.version('{package_name}'))",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            self.logger.debug("importlib.metadata failed for %s: %s", package_name, e)

        # Fallback: pip show
        try:
            result = subprocess.run(
                ["pip", "show", package_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if line.startswith("Version:"):
                        return line.split(":", 1)[1].strip()
        except Exception as e:
            self.logger.debug("pip show failed for %s: %s", package_name, e)

        return None
