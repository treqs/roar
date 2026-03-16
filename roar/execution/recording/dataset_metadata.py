"""Helpers for attaching dataset identity labels to composite artifact metadata."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def find_matching_identifier(
    root_path: str, dataset_identifiers: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Match a composite root path to its dataset identifier by ``file://`` URI.

    Compares the path component of each identifier's ``dataset_id`` against
    *root_path* after stripping trailing slashes from both sides.

    Returns the first matching identifier dict, or ``None``.
    """
    normalized_root = root_path.rstrip("/")
    for identifier in dataset_identifiers:
        dataset_id = identifier.get("dataset_id", "")
        parsed = urlparse(dataset_id)
        if parsed.scheme != "file":
            continue
        id_path = parsed.path.rstrip("/")
        if id_path == normalized_root:
            return identifier
    return None


def build_dataset_metadata(identifier: dict[str, Any]) -> dict[str, Any]:
    """Extract dataset label fields from an identifier dict.

    Returns a plain dict suitable for JSON serialization inside artifact
    metadata under the ``"dataset"`` key.
    """
    meta: dict[str, Any] = {}
    for key in (
        "dataset_id",
        "dataset_fingerprint",
        "dataset_fingerprint_algorithm",
        "confidence",
        "evidence",
        "split",
        "version_hint",
        "observed_paths",
        "profile",
    ):
        value = identifier.get(key)
        if value is not None:
            meta[key] = value
    return meta
