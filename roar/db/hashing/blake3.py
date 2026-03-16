"""Convenience helpers for BLAKE3 hashing operations."""

from __future__ import annotations

from pathlib import Path

from .backend import compute_hashes_batch


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
