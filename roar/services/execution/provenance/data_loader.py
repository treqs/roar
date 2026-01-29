"""
Data loader service for provenance collection.

Loads tracer JSON output and Python inject data with proper error handling.
"""

import json
import os
import sys

from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import PythonInjectData, TracerData


class DataLoaderService:
    """Loads tracer and Python inject data from JSON files."""

    def __init__(self, logger: ILogger | None = None) -> None:
        """Initialize data loader with optional logger."""
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

    def load_tracer_data(self, path: str) -> TracerData:
        """
        Load tracer JSON output.

        Args:
            path: Path to the tracer JSON file

        Returns:
            TracerData with parsed values

        Raises:
            FileNotFoundError: If the tracer file doesn't exist
            json.JSONDecodeError: If the file is not valid JSON
        """
        self.logger.debug("Loading tracer data from: %s", path)
        with open(path) as f:
            data = json.load(f)

        self.logger.debug("Tracer JSON parsed successfully: %d keys", len(data))
        return TracerData(
            opened_files=data.get("opened_files", []),
            read_files=data.get("read_files", []),
            written_files=data.get("written_files", []),
            processes=data.get("processes", []),
            start_time=data.get("start_time", 0),
            end_time=data.get("end_time", 0),
        )

    def load_python_data(self, path: str | None) -> PythonInjectData:
        """
        Load Python inject JSON output (optional).

        Args:
            path: Path to the Python inject JSON file, or None

        Returns:
            PythonInjectData with parsed values (defaults if file missing/invalid)
        """
        self.logger.debug("Loading Python inject data from: %s", path)
        if not path or not os.path.exists(path):
            self.logger.debug("Python inject file not found, using defaults")
            return PythonInjectData(
                sys_prefix=sys.prefix,
                sys_base_prefix=sys.base_prefix,
            )

        try:
            with open(path) as f:
                data = json.load(f)
            self.logger.debug("Python inject JSON parsed successfully")
        except (OSError, json.JSONDecodeError) as e:
            self.logger.debug("Failed to parse Python inject data: %s, using defaults", e)
            return PythonInjectData(
                sys_prefix=sys.prefix,
                sys_base_prefix=sys.base_prefix,
            )

        return PythonInjectData(
            modules_files=data.get("modules_files", []),
            env_reads=data.get("env_reads", {}),
            sys_prefix=data.get("sys_prefix", sys.prefix),
            sys_base_prefix=data.get("sys_base_prefix", sys.base_prefix),
            roar_inject_dir=data.get("roar_inject_dir", ""),
            shared_libs=data.get("shared_libs", []),
            used_packages=data.get("used_packages", {}),
            installed_packages=data.get("installed_packages", {}),
        )
