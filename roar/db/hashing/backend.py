"""
Unified artifact hashing backend.

Uses the native Rust module when available, and falls back to Python
implementations for development environments where the extension has
not been built yet.
"""

from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_hash_native: Any | None
try:
    _hash_native = importlib.import_module("roar._hash_native")
except Exception:
    _hash_native = None

try:
    import blake3 as _blake3

    blake3: Any | None = _blake3
except Exception:
    blake3 = None

VALID_ALGORITHMS = ("blake3", "sha256", "sha512", "md5")
_DEFAULT_ALGORITHMS = ("blake3",)
_READ_CHUNK_SIZE = 8 * 1024 * 1024
_AUTO_WORKERS_MIN_FILES = 8
_AUTO_WORKERS_HIGH_FILES = 64
_AUTO_WORKERS_SAMPLE_SIZE = 64
_AUTO_WORKERS_MAX = 8
_AUTO_WORKERS_DEFAULT = 4
_AUTO_WORKERS_SMALL_AVG_THRESHOLD = 1024 * 1024
_AUTO_WORKERS_MEDIUM_AVG_THRESHOLD = 8 * 1024 * 1024


def normalize_algorithms(
    algorithms: Iterable[str] | None,
    default: tuple[str, ...] = _DEFAULT_ALGORITHMS,
) -> list[str]:
    """Validate and deduplicate algorithms while preserving input order."""
    requested = list(algorithms) if algorithms is not None else list(default)
    normalized: list[str] = []
    for algo in requested:
        if algo not in VALID_ALGORITHMS:
            valid = ", ".join(sorted(VALID_ALGORITHMS))
            raise ValueError(f"Unknown hash algorithm: {algo}. Valid algorithms: {valid}")
        if algo not in normalized:
            normalized.append(algo)
    return normalized


def compute_hash(path: str | Path, algorithm: str = "blake3") -> str | None:
    """Compute one hash digest for a file."""
    hashes = compute_hashes(path, [algorithm])
    if not hashes:
        return None
    return hashes.get(algorithm)


def compute_hashes(
    path: str | Path, algorithms: Iterable[str] | None = None
) -> dict[str, str] | None:
    """
    Compute one or more hashes for a file in a single pass.

    Returns None when the file cannot be read.
    """
    normalized = normalize_algorithms(algorithms)
    path_str = str(path)

    if _hash_native is not None:
        try:
            return _hash_native.compute_hashes(path_str, normalized)
        except OSError:
            return None

    path_obj = Path(path_str)
    hashers = _create_python_hashers(normalized)
    try:
        with path_obj.open("rb") as f:
            for chunk in iter(lambda: f.read(_READ_CHUNK_SIZE), b""):
                for hasher in hashers.values():
                    hasher.update(chunk)
    except OSError:
        return None

    return {algo: hasher.hexdigest() for algo, hasher in hashers.items()}


def compute_hashes_batch(
    paths: Iterable[str | Path],
    algorithms: Iterable[str] | None = None,
    workers: int | None = None,
) -> dict[str, dict[str, str]]:
    """Compute hashes for multiple files, optionally in native parallel mode."""
    normalized = normalize_algorithms(algorithms)
    string_paths = [str(path) for path in paths]
    hashable_paths = [path for path in string_paths if os.path.isfile(path)]
    if not hashable_paths:
        return {}

    if _hash_native is not None:
        selected_workers = _resolve_native_workers(hashable_paths, workers)
        return _hash_native.compute_hashes_batch(hashable_paths, normalized, selected_workers)

    output: dict[str, dict[str, str]] = {}
    for path in hashable_paths:
        if hashes := compute_hashes(path, normalized):
            output[path] = hashes
    return output


def _resolve_native_workers(paths: list[str], workers: int | None) -> int | None:
    """
    Resolve worker count for native batch hashing.

    Explicit `workers` always wins. When omitted, apply a lightweight heuristic:
    enable parallelism only for larger batches to avoid overhead on tiny workloads.
    """
    if workers is not None:
        return workers

    cpu_count = os.cpu_count() or 1
    if cpu_count < 2:
        return None

    path_count = len(paths)
    if path_count < _AUTO_WORKERS_MIN_FILES:
        return None

    if path_count >= _AUTO_WORKERS_HIGH_FILES:
        return min(cpu_count, _AUTO_WORKERS_MAX)

    sample = paths[:_AUTO_WORKERS_SAMPLE_SIZE]
    total_bytes = 0
    sampled_files = 0
    for path in sample:
        try:
            total_bytes += os.path.getsize(path)
            sampled_files += 1
        except OSError:
            continue

    if sampled_files == 0:
        return min(cpu_count, _AUTO_WORKERS_DEFAULT)

    avg_size = total_bytes / sampled_files
    if avg_size <= _AUTO_WORKERS_SMALL_AVG_THRESHOLD:
        return min(cpu_count, _AUTO_WORKERS_DEFAULT)
    if avg_size <= _AUTO_WORKERS_MEDIUM_AVG_THRESHOLD and path_count >= 16:
        return min(cpu_count, _AUTO_WORKERS_DEFAULT)
    if path_count >= 32:
        return min(cpu_count, 2)
    return None


def _create_python_hashers(algorithms: Iterable[str]) -> dict[str, Any]:
    """Create Python hashers for requested algorithms."""
    hashers: dict[str, Any] = {}
    for algo in algorithms:
        if algo == "blake3":
            if blake3 is None:
                raise ValueError("blake3 algorithm requested but blake3 package is not installed")
            hashers[algo] = blake3.blake3()
        elif algo == "sha256":
            hashers[algo] = hashlib.sha256()
        elif algo == "sha512":
            hashers[algo] = hashlib.sha512()
        elif algo == "md5":
            hashers[algo] = hashlib.md5()
        else:
            valid = ", ".join(sorted(VALID_ALGORITHMS))
            raise ValueError(f"Unknown hash algorithm: {algo}. Valid algorithms: {valid}")
    return hashers
