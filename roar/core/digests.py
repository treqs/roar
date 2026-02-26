"""Digest selection helpers shared across lineage and registration flows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PRIMARY_DIGEST_ALGORITHMS: tuple[str, ...] = ("blake3", "composite-blake3")


def extract_primary_digest(
    item: Mapping[str, Any] | None,
    preferred_algorithms: Sequence[str] = PRIMARY_DIGEST_ALGORITHMS,
) -> str | None:
    """
    Return the best available digest from an artifact-like mapping.

    Selection order:
    1. First digest matching any preferred algorithm (in provided order)
    2. First non-empty digest in the hash list
    """
    if not isinstance(item, Mapping):
        return None

    hashes = item.get("hashes", [])
    if not isinstance(hashes, list):
        return None

    normalized_preferences = tuple(
        algorithm.strip().lower()
        for algorithm in preferred_algorithms
        if isinstance(algorithm, str) and algorithm.strip()
    )

    for preferred_algorithm in normalized_preferences:
        for hash_entry in hashes:
            if not isinstance(hash_entry, Mapping):
                continue
            algorithm = hash_entry.get("algorithm")
            digest = hash_entry.get("digest")
            if (
                isinstance(algorithm, str)
                and algorithm.strip().lower() == preferred_algorithm
                and isinstance(digest, str)
                and digest
            ):
                return digest

    for hash_entry in hashes:
        if not isinstance(hash_entry, Mapping):
            continue
        digest = hash_entry.get("digest")
        if isinstance(digest, str) and digest:
            return digest

    return None
