"""Shared constants for system-managed label paths."""

from __future__ import annotations

AUTO_DATASET_LABEL_KEYS = frozenset(
    {
        "dataset.type",
        "dataset.id",
        "dataset.fingerprint",
        "dataset.fingerprint_algorithm",
        "dataset.split",
        "dataset.version_hint",
        "dataset.modality",
    }
)

TAG_NAMESPACE = "tag"

CANONICAL_TAG_KINDS: frozenset[str] = frozenset(
    {
        "license",
        "contains_pii",
        "jurisdiction",
        "classification",
        "special_category",
    }
)
