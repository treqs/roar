"""
Default hashing service implementation.

Provides file hashing with caching support.
"""

import os

from ...core.interfaces.repositories import HashCacheRepository
from ...core.interfaces.services import HashingService
from ..hashing.backend import (
    compute_hashes_batch as compute_hashes_batch_backend,
)
from ..hashing.backend import (
    normalize_algorithms,
)


class DefaultHashingService(HashingService):
    """
    Default implementation of hashing service.

    Uses a native-backed hashing backend with repository-based cache
    for performance optimization.
    """

    def __init__(self, hash_cache: HashCacheRepository):
        """
        Initialize hashing service.

        Args:
            hash_cache: Repository for caching hashes
        """
        self._hash_cache = hash_cache

    def compute_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """Compute a single hash for a file."""
        selected = normalize_algorithms([algorithm])[0]
        batch = self.compute_hashes_batch([path], [selected])
        return batch.get(path, {}).get(selected)

    def compute_file_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """
        Compute a single hash for a file.

        Alias for compute_hash() for backward compatibility.

        Args:
            path: File path
            algorithm: Hash algorithm ('blake3', 'sha256', 'sha512', 'md5')

        Returns:
            Hash digest, or None if file doesn't exist.
        """
        return self.compute_hash(path, algorithm)

    def compute_hashes(
        self, path: str, algorithms: list[str] | None = None
    ) -> dict[str, str] | None:
        """Compute multiple hashes for a file in a single pass."""
        batch = self.compute_hashes_batch([path], algorithms)
        return batch.get(path)

    def compute_hashes_batch(
        self,
        paths: list[str],
        algorithms: list[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """
        Compute hashes for multiple files in batch.

        Returns:
            Dict[path] -> {algorithm: digest}. Files that are missing/unreadable
            are omitted from the result.
        """
        normalized = normalize_algorithms(algorithms)

        # De-duplicate while preserving order.
        unique_paths: list[str] = []
        seen_paths: set[str] = set()
        for path in paths:
            if path not in seen_paths:
                seen_paths.add(path)
                unique_paths.append(path)

        fully_cached: dict[str, dict[str, str]] = {}
        to_compute: list[str] = []
        stats_by_path: dict[str, tuple[int, float]] = {}
        partial_cache: dict[str, dict[str, str]] = {}

        for path in unique_paths:
            try:
                stat = os.stat(path)
            except OSError:
                continue

            cached = self._hash_cache.get_cached_hashes(path)
            missing = [algo for algo in normalized if algo not in cached]

            if not missing:
                fully_cached[path] = {algo: cached[algo] for algo in normalized}
                continue

            to_compute.append(path)
            stats_by_path[path] = (stat.st_size, stat.st_mtime)
            partial_cache[path] = dict(cached)

        if not to_compute:
            return fully_cached

        computed_batch = compute_hashes_batch_backend(to_compute, normalized)
        if not computed_batch:
            return fully_cached

        output = dict(fully_cached)
        for path in to_compute:
            computed = computed_batch.get(path)
            if not computed:
                continue

            size, mtime = stats_by_path[path]
            self._hash_cache.cache_hashes(path, computed, size, mtime)

            merged = partial_cache.get(path, {})
            merged.update(computed)
            if all(algo in merged for algo in normalized):
                output[path] = {algo: merged[algo] for algo in normalized}

        return output

    def get_cached_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """
        Get cached hash for a file if still valid.

        Args:
            path: File path
            algorithm: Hash algorithm

        Returns:
            Cached hash digest or None if not cached/stale.
        """
        return self._hash_cache.get_cached_hash(path, algorithm)

    def get_cached_hashes(self, path: str) -> dict[str, str]:
        """
        Get all cached hashes for a file.

        Args:
            path: File path

        Returns:
            Dict of {algorithm: digest} or empty dict.
        """
        return self._hash_cache.get_cached_hashes(path)

    def invalidate_cache(self, path: str, algorithm: str | None = None) -> None:
        """
        Invalidate cached hashes for a file.

        Args:
            path: File path
            algorithm: Specific algorithm or None for all
        """
        self._hash_cache.invalidate(path, algorithm)

    def clean_stale_cache(self, max_age_days: int = 30) -> None:
        """
        Remove old cache entries.

        Args:
            max_age_days: Maximum age in days
        """
        self._hash_cache.clean_stale(max_age_days)
