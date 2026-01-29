"""
SQLAlchemy hash cache repository implementation.

Handles caching of file hashes to avoid redundant computations.
"""

import os
import time

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...core.interfaces.repositories import HashCacheRepository
from ..models import HashCache


class SQLAlchemyHashCacheRepository(HashCacheRepository):
    """
    SQLAlchemy implementation of hash cache repository.

    Caches file hashes with metadata (size, mtime) to detect
    when recalculation is needed.
    """

    def __init__(self, session: Session):
        """
        Initialize repository with database session.

        Args:
            session: SQLAlchemy session
        """
        self._session = session

    def get_cached_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        """
        Get cached hash for a file if still valid (mtime/size match).

        Args:
            path: File path to check
            algorithm: Hash algorithm ('blake3', 'sha256', 'sha512', 'md5')

        Returns:
            Hash digest if cache is valid, None otherwise.
        """
        try:
            stat = os.stat(path)
            current_size = stat.st_size
            current_mtime = stat.st_mtime
        except OSError:
            return None

        entry = self._session.execute(
            select(HashCache).where(HashCache.path == path, HashCache.algorithm == algorithm)
        ).scalar_one_or_none()

        if entry and entry.size == current_size and abs(entry.mtime - current_mtime) < 0.001:
            return entry.digest

        return None

    def get_cached_hashes(self, path: str) -> dict[str, str]:
        """
        Get all cached hashes for a file if still valid.

        Args:
            path: File path to check

        Returns:
            Dict of {algorithm: digest} or empty dict if stale/missing.
        """
        try:
            stat = os.stat(path)
            current_size = stat.st_size
            current_mtime = stat.st_mtime
        except OSError:
            return {}

        entries = (
            self._session.execute(select(HashCache).where(HashCache.path == path)).scalars().all()
        )

        result = {}
        for entry in entries:
            if entry.size == current_size and abs(entry.mtime - current_mtime) < 0.001:
                result[entry.algorithm] = entry.digest
        return result

    def cache_hash(self, path: str, algorithm: str, digest: str, size: int, mtime: float) -> None:
        """
        Store a hash in the cache.

        Args:
            path: File path
            algorithm: Hash algorithm
            digest: Hash digest
            size: File size in bytes
            mtime: File modification time
        """
        existing = self._session.execute(
            select(HashCache).where(HashCache.path == path, HashCache.algorithm == algorithm)
        ).scalar_one_or_none()

        if existing:
            existing.digest = digest
            existing.size = size
            existing.mtime = mtime
            existing.cached_at = time.time()
        else:
            entry = HashCache(
                path=path,
                algorithm=algorithm,
                digest=digest,
                size=size,
                mtime=mtime,
                cached_at=time.time(),
            )
            self._session.add(entry)
        self._session.flush()

    def cache_hashes(self, path: str, hashes: dict[str, str], size: int, mtime: float) -> None:
        """
        Store multiple hashes for a file.

        Args:
            path: File path
            hashes: Dict of {algorithm: digest}
            size: File size in bytes
            mtime: File modification time
        """
        now = time.time()
        for algo, digest in hashes.items():
            existing = self._session.execute(
                select(HashCache).where(HashCache.path == path, HashCache.algorithm == algo)
            ).scalar_one_or_none()

            if existing:
                existing.digest = digest
                existing.size = size
                existing.mtime = mtime
                existing.cached_at = now
            else:
                entry = HashCache(
                    path=path,
                    algorithm=algo,
                    digest=digest,
                    size=size,
                    mtime=mtime,
                    cached_at=now,
                )
                self._session.add(entry)
        self._session.flush()

    def invalidate(self, path: str, algorithm: str | None = None) -> None:
        """
        Remove a file from the hash cache.

        Args:
            path: File path
            algorithm: Specific algorithm to invalidate, or None for all
        """
        if algorithm:
            self._session.execute(
                delete(HashCache).where(HashCache.path == path, HashCache.algorithm == algorithm)
            )
        else:
            self._session.execute(delete(HashCache).where(HashCache.path == path))
        self._session.flush()

    def clean_stale(self, max_age_days: int = 30) -> None:
        """
        Remove hash cache entries older than max_age_days.

        Args:
            max_age_days: Maximum age in days before entries are removed
        """
        cutoff = time.time() - (max_age_days * 86400)
        self._session.execute(delete(HashCache).where(HashCache.cached_at < cutoff))
        self._session.flush()


# Backward compatibility alias
SQLiteHashCacheRepository = SQLAlchemyHashCacheRepository
