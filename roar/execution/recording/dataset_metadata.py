"""Helpers for attaching dataset identity metadata and labels to composite artifacts."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ...core.label_constants import AUTO_DATASET_LABEL_KEYS
from .dataset_profile import build_dataset_profile

__all__ = [
    "AUTO_DATASET_LABEL_KEYS",
    "build_dataset_label_metadata",
    "build_dataset_metadata",
    "find_matching_identifier",
]


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


def build_dataset_label_metadata(
    identifier: dict[str, Any],
    *,
    components: list[dict[str, Any]] | None = None,
    component_count_total: int | None = None,
) -> dict[str, Any]:
    """Build the system-managed label document for a detected dataset artifact.

    The label payload is intentionally smaller and more stable than the full
    dataset metadata blob. It captures the artifact's dataset identity and the
    most queryable derived characteristics for local labels and future sync.
    """
    dataset: dict[str, Any] = {"type": "dataset"}

    value = identifier.get("dataset_id")
    if value is not None:
        dataset["id"] = value

    value = identifier.get("dataset_fingerprint")
    if value is not None:
        dataset["fingerprint"] = value

    value = identifier.get("dataset_fingerprint_algorithm")
    if value is not None:
        dataset["fingerprint_algorithm"] = value

    value = identifier.get("split")
    if value is not None:
        dataset["split"] = value

    value = identifier.get("version_hint")
    if value is not None:
        dataset["version_hint"] = value

    profile = build_dataset_profile(components or [], total_components=component_count_total)
    modality = profile.get("modality_hint") if isinstance(profile, dict) else None
    if isinstance(modality, str) and modality:
        dataset["modality"] = modality

    return {"dataset": dataset}
