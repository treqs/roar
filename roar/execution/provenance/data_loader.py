"""
Data loader service for provenance collection.

Loads tracer output (MessagePack) and Python inject data with proper error handling.
"""

import json
import os
import sys

import msgpack

from ...core.interfaces.logger import ILogger
from ...core.models.provenance import PythonInjectData, TracerData


class DataLoaderService:
    """Loads tracer and Python inject data."""

    def __init__(self, logger: ILogger | None = None) -> None:
        """Initialize data loader with optional logger."""
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def load_tracer_data(self, path: str) -> TracerData:
        """
        Load tracer output (MessagePack).

        Args:
            path: Path to the tracer MessagePack file

        Returns:
            TracerData with parsed values

        Raises:
            FileNotFoundError: If the tracer file doesn't exist
        """
        self.logger.debug("Loading tracer data from: %s", path)
        with open(path, "rb") as f:
            payload = f.read()

        data = self._parse_tracer_payload(payload, path)

        self.logger.debug("Tracer data parsed successfully: %d keys", len(data))
        files = self._normalize_files(data)
        opened_files, read_files, written_files = self._derive_file_lists(data, files)

        return TracerData(
            opened_files=opened_files,
            read_files=read_files,
            written_files=written_files,
            files=files,
            processes=data.get("processes", []),
            start_time=float(data.get("start_time", 0)),
            end_time=float(data.get("end_time", 0)),
            version=int(data.get("version", 1) or 1),
            tracer_mode=str(data.get("tracer_mode", "ptrace") or "ptrace"),
            events_dropped=int(data.get("events_dropped", 0) or 0),
        )

    def _parse_tracer_payload(self, payload: bytes, path: str) -> dict:
        """Parse the tracer report from MessagePack or a legacy JSON payload."""
        stripped = payload.lstrip()
        if stripped.startswith((b"{", b"[")):
            self.logger.warning(
                "Tracer report at %s is JSON, not MessagePack; accepting legacy format", path
            )
            data = json.loads(payload.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Expected tracer report object in {path}")
            return data

        data = msgpack.unpackb(payload, raw=False)
        if not isinstance(data, dict):
            raise ValueError(f"Expected tracer report object in {path}")
        return data

    def _normalize_files(self, data: dict) -> list[dict]:
        """Normalize tracer file records to a common shape."""
        normalized: list[dict] = []

        raw_files = data.get("files", [])
        if isinstance(raw_files, list) and raw_files:
            for record in raw_files:
                if not isinstance(record, dict):
                    continue
                path = record.get("path")
                if not isinstance(path, str) or not path:
                    continue

                item = {
                    "path": path,
                    "read": bool(record.get("read", False)),
                    "written": bool(record.get("written", False)),
                }
                for key in ("read_threads", "written_threads"):
                    value = record.get(key)
                    if isinstance(value, list):
                        item[key] = [thread for thread in value if isinstance(thread, int)]
                if "chunks_read" in record:
                    item["chunks_read"] = record.get("chunks_read")
                if "chunks_written" in record:
                    item["chunks_written"] = record.get("chunks_written")
                normalized.append(item)
            return normalized

        # Legacy ptrace reports don't have "files"; synthesize from aggregate lists.
        opened = data.get("opened_files", [])
        read = set(data.get("read_files", []))
        written = set(data.get("written_files", []))
        paths = []
        if isinstance(opened, list):
            paths.extend(opened)
        paths.extend([p for p in read if p not in paths])
        paths.extend([p for p in written if p not in paths])

        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            normalized.append(
                {
                    "path": path,
                    "read": path in read,
                    "written": path in written,
                }
            )

        return normalized

    def _derive_file_lists(
        self,
        data: dict,
        files: list[dict],
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Derive opened/read/written lists from whichever tracer format was provided.

        Preference order:
        1. Explicit legacy arrays when present
        2. Derived values from normalized file records
        """

        def _explicit_list(key: str) -> list[str] | None:
            value = data.get(key)
            if not isinstance(value, list):
                return None
            return [item for item in value if isinstance(item, str)]

        explicit_opened = _explicit_list("opened_files")
        explicit_read = _explicit_list("read_files")
        explicit_written = _explicit_list("written_files")

        if (
            explicit_opened is not None
            and explicit_read is not None
            and explicit_written is not None
        ):
            return explicit_opened, explicit_read, explicit_written

        opened_files: list[str] = []
        read_files: list[str] = []
        written_files: list[str] = []
        for record in files:
            path = record.get("path")
            if not isinstance(path, str):
                continue
            opened_files.append(path)
            if record.get("read"):
                read_files.append(path)
            if record.get("written"):
                written_files.append(path)
        return opened_files, read_files, written_files

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
