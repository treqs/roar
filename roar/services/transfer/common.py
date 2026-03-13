"""Shared transfer-layer utilities for get/put services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ...db.hashing.backend import compute_hashes_batch


class DatabaseContext(Protocol):
    """Protocol for database context dependency."""

    @property
    def artifacts(self) -> Any: ...

    @property
    def jobs(self) -> Any: ...

    @property
    def sessions(self) -> Any: ...

    @property
    def labels(self) -> Any: ...


def hash_files_blake3(paths: list[Path]) -> dict[str, str]:
    """Compute BLAKE3 hashes for paths in one backend batch call."""
    if not paths:
        return {}

    raw_result = compute_hashes_batch(paths, ["blake3"])
    result: dict[str, str] = {}
    for path in paths:
        key = str(path)
        digest = raw_result.get(key, {}).get("blake3")
        if digest:
            result[key] = digest
    return result
def build_operation_metadata_json(operation: str, payload: dict[str, Any]) -> str:
    """Wrap operation payload in a namespaced metadata object and serialize to JSON."""
    return json.dumps({operation: payload})
