"""
Hash algorithm strategy implementations.

Each strategy encapsulates the logic for a specific hash algorithm,
following the Strategy pattern for extensibility.
"""

import hashlib
from abc import ABC, abstractmethod
from typing import Any

try:
    import blake3 as _blake3

    blake3: Any | None = _blake3
except ImportError:
    blake3 = None


class HashStrategy(ABC):
    """
    Abstract base class for hash algorithm strategies.

    Implementations must provide:
    - algorithm_name: Unique identifier for the algorithm
    - create_hasher(): Factory method for hasher instances
    - update(): Method to add data to hasher
    - hexdigest(): Method to get final hash
    """

    @property
    @abstractmethod
    def algorithm_name(self) -> str:
        """Return algorithm identifier (e.g., 'blake3', 'sha256')."""
        pass

    @abstractmethod
    def create_hasher(self) -> Any:
        """Create a new hasher instance."""
        pass

    def update(self, hasher: Any, data: bytes) -> None:
        """Update hasher with data. Default implementation works for most hashers."""
        hasher.update(data)

    def hexdigest(self, hasher: Any) -> str:
        """Get hex digest from hasher. Default implementation works for most hashers."""
        return hasher.hexdigest()


class Blake3Strategy(HashStrategy):
    """BLAKE3 hashing strategy - fast cryptographic hash."""

    @property
    def algorithm_name(self) -> str:
        return "blake3"

    def create_hasher(self) -> Any:
        if blake3 is None:
            raise ImportError("blake3 package not installed")
        return blake3.blake3()


class SHA256Strategy(HashStrategy):
    """SHA-256 hashing strategy - widely compatible."""

    @property
    def algorithm_name(self) -> str:
        return "sha256"

    def create_hasher(self) -> Any:
        return hashlib.sha256()


class SHA512Strategy(HashStrategy):
    """SHA-512 hashing strategy - stronger variant of SHA-2."""

    @property
    def algorithm_name(self) -> str:
        return "sha512"

    def create_hasher(self) -> Any:
        return hashlib.sha512()


class MD5Strategy(HashStrategy):
    """MD5 hashing strategy - for legacy compatibility only."""

    @property
    def algorithm_name(self) -> str:
        return "md5"

    def create_hasher(self) -> Any:
        return hashlib.md5()
