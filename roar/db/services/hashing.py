"""
Default hashing service implementation.

Provides file hashing with caching support.
"""

import os

from ...core.interfaces.repositories import HashCacheRepository
from ...core.interfaces.services import HashingService
from ..hashing import HashAlgorithmRegistry


class DefaultHashingService(HashingService):
    """
    Default implementation of hashing service.

    Uses hash algorithm registry for pluggable hash algorithms
    and hash cache repository for performance optimization.
    """

    def __init__(
        self, hash_cache: HashCacheRepository, registry: HashAlgorithmRegistry | None = None
    ):
        """
        Initialize hashing service.

        Args:
            hash_cache: Repository for caching hashes
            registry: Hash algorithm registry (defaults to standard registry)
        """
        self._hash_cache = hash_cache
        self._registry = registry or HashAlgorithmRegistry()

    def compute_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """
        Compute a single hash for a file.

        Uses cache if available, computes and caches if not.

        Args:
            path: File path
            algorithm: Hash algorithm ('blake3', 'sha256', 'sha512', 'md5')

        Returns:
            Hash digest, or None if file doesn't exist.
        """
        try:
            stat = os.stat(path)
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            return None

        # Check cache first
        cached = self._hash_cache.get_cached_hash(path, algorithm)
        if cached:
            return cached

        # Compute hash
        hasher = self._registry.create_hasher(algorithm)
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192 * 1024), b""):  # 8MB chunks
                    hasher.update(chunk)
        except OSError:
            return None

        digest = hasher.hexdigest()

        # Cache it
        self._hash_cache.cache_hash(path, algorithm, digest, size, mtime)

        return digest

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
        """
        Compute multiple hashes for a file in a single pass.

        Args:
            path: File path
            algorithms: List of algorithms. Defaults to ['blake3'].

        Returns:
            Dict of {algorithm: digest}, or None if file doesn't exist.
        """
        if algorithms is None:
            algorithms = ["blake3"]

        try:
            stat = os.stat(path)
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            return None

        # Check what's already cached
        cached = self._hash_cache.get_cached_hashes(path)
        needed = [algo for algo in algorithms if algo not in cached]

        if not needed:
            # All algorithms cached
            return {algo: cached[algo] for algo in algorithms}

        # Compute missing hashes in single pass
        hashers = {algo: self._registry.create_hasher(algo) for algo in needed}
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192 * 1024), b""):  # 8MB chunks
                    for hasher in hashers.values():
                        hasher.update(chunk)
        except OSError:
            return None

        # Collect results and cache new hashes
        new_hashes = {}
        for algo, hasher in hashers.items():
            digest = hasher.hexdigest()
            cached[algo] = digest
            new_hashes[algo] = digest

        # Cache new hashes
        if new_hashes:
            self._hash_cache.cache_hashes(path, new_hashes, size, mtime)

        return {algo: cached[algo] for algo in algorithms}

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
