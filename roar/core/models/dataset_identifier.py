"""
Dataset identifier contract models.

Defines the normalized shape emitted by path-derived dataset inference.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from .base import RoarBaseModel

DatasetFingerprintAlgorithm = Literal["blake3", "sha256"]
DatasetSplit = Literal["train", "val", "test", "dev", "predict", "holdout"]

_SUPPORTED_DATASET_SCHEMES = frozenset({"file", "s3", "gs", "https", "http"})

# Canonical split name mapping — covers both canonical forms and common aliases.
# Used by inference (dataset_identifier) and profiling (_dataset_profile).
SPLIT_CANONICAL_MAP: dict[str, str] = {
    "train": "train",
    "training": "train",
    "val": "val",
    "validation": "val",
    "test": "test",
    "dev": "dev",
    "eval": "dev",
    "predict": "predict",
    "holdout": "holdout",
}

# All recognised split segment names (keys of SPLIT_CANONICAL_MAP).
SPLIT_SEGMENTS: frozenset[str] = frozenset(SPLIT_CANONICAL_MAP)


class DatasetIdentifier(RoarBaseModel):
    """Stable dataset identifier payload emitted by inference pipelines."""

    dataset_id: Annotated[str, Field(min_length=1, max_length=4096)]
    dataset_fingerprint: Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[a-f0-9]+$")]
    dataset_fingerprint_algorithm: DatasetFingerprintAlgorithm
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    evidence: list[Annotated[str, Field(min_length=1, max_length=128)]]
    observed_paths: Annotated[int, Field(ge=1)]
    split: DatasetSplit | None = None
    version_hint: Annotated[str, Field(min_length=1, max_length=128)] | None = None

    @field_validator("dataset_id")
    @classmethod
    def validate_dataset_id(cls, value: str) -> str:
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        if scheme not in _SUPPORTED_DATASET_SCHEMES:
            raise ValueError(f"Unsupported dataset_id scheme: {scheme}")

        if scheme == "file":
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                raise ValueError("file dataset_id must not include a host component")
            if not parsed.path.startswith("/"):
                raise ValueError("file dataset_id must contain an absolute path")
            return value

        if not parsed.netloc:
            raise ValueError(f"{scheme} dataset_id must include a bucket/host component")
        return value
