"""Artifact hashing backend API."""

from .backend import (
    VALID_ALGORITHMS,
    compute_hash,
    compute_hashes,
    compute_hashes_batch,
    normalize_algorithms,
)
from .blake3 import hash_files_blake3

__all__ = [
    "VALID_ALGORITHMS",
    "compute_hash",
    "compute_hashes",
    "compute_hashes_batch",
    "hash_files_blake3",
    "normalize_algorithms",
]
