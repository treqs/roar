"""
Shared artifact-reference helpers.

Centralises the hash-extraction logic previously duplicated across
coordinator.py, artifact.py, job.py and put/service.py.
"""

from __future__ import annotations

from typing import Any


def cache_key(item: dict[str, Any]) -> str | None:
    """Derive a stable cache key from an artifact reference dict.

    Checks ``hash`` (single string) first, then falls back to the first
    entry in ``hashes`` (list of ``{algorithm, digest}`` dicts).
    """
    hash_value = item.get("hash")
    if isinstance(hash_value, str) and hash_value:
        return f"hash:{hash_value.lower()}"

    hashes = item.get("hashes")
    if isinstance(hashes, list):
        for entry in hashes:
            if not isinstance(entry, dict):
                continue
            algorithm = entry.get("algorithm")
            digest = entry.get("digest")
            if isinstance(algorithm, str) and isinstance(digest, str) and digest:
                return f"{algorithm.lower()}:{digest.lower()}"

    return None


def preview(item: dict[str, Any]) -> str | None:
    """Best-effort short preview string for logging an artifact reference.

    Returns at most the first 12 characters of a digest, falling back to
    ``artifact_hash``, ``artifact_id`` and finally ``path``.
    """
    artifact_hash = item.get("artifact_hash")
    if isinstance(artifact_hash, str) and artifact_hash:
        return artifact_hash[:12]

    artifact_id = item.get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id:
        return artifact_id[:12]

    hash_value = item.get("hash")
    if isinstance(hash_value, str) and hash_value:
        return hash_value[:12]

    hashes = item.get("hashes")
    if isinstance(hashes, list) and hashes:
        first = hashes[0]
        if isinstance(first, dict):
            digest = first.get("digest")
            if isinstance(digest, str) and digest:
                return digest[:12]

    path = item.get("path")
    if isinstance(path, str) and path:
        return path

    return None


def extract_digest(item: dict[str, Any]) -> str | None:
    """Extract a canonical digest from an artifact reference.

    Prefers ``hash`` (single string), then ``blake3`` from ``hashes``, then
    falls back to the first digest in ``hashes``.
    """
    hash_value = item.get("hash")
    if isinstance(hash_value, str) and hash_value:
        return hash_value.lower()

    hashes_value = item.get("hashes")
    if isinstance(hashes_value, list):
        for candidate in hashes_value:
            if not isinstance(candidate, dict):
                continue
            algorithm = candidate.get("algorithm")
            digest = candidate.get("digest")
            if (
                isinstance(algorithm, str)
                and algorithm.lower() == "blake3"
                and isinstance(digest, str)
            ):
                return digest.lower()

        if hashes_value:
            first = hashes_value[0]
            if isinstance(first, dict):
                digest = first.get("digest")
                if isinstance(digest, str) and digest:
                    return digest.lower()

    return None


def artifact_path(artifact: dict[str, Any]) -> str | None:
    """Return the best available path string from an artifact dict.

    Prefers ``first_seen_path`` (local DB field) and falls back to ``path``
    (used in some GLaaS response shapes and lineage dicts).
    """
    path = artifact.get("first_seen_path") or artifact.get("path")
    return path if isinstance(path, str) and path else None
