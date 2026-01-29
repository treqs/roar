"""
Hash algorithm registry.

Provides a registry for hash algorithm strategies, enabling
new algorithms to be added without modifying existing code
(Open/Closed Principle).
"""

from typing import Any

from .strategies import (
    Blake3Strategy,
    HashStrategy,
    MD5Strategy,
    SHA256Strategy,
    SHA512Strategy,
)


class HashAlgorithmRegistry:
    """
    Registry for hash algorithm strategies.

    Follows Open/Closed Principle: add new algorithms by registering
    new strategies without modifying existing code.

    Example:
        registry = HashAlgorithmRegistry()

        # Use default algorithms
        hasher = registry.create_hasher("blake3")

        # Register custom algorithm
        registry.register(MyCustomStrategy())
        hasher = registry.create_hasher("my_custom")
    """

    def __init__(self, register_defaults: bool = True):
        """
        Initialize the registry.

        Args:
            register_defaults: If True, register built-in algorithms
        """
        self._strategies: dict[str, HashStrategy] = {}
        if register_defaults:
            self._register_defaults()

    def _register_defaults(self) -> None:
        """Register built-in hash algorithms."""
        self.register(Blake3Strategy())
        self.register(SHA256Strategy())
        self.register(SHA512Strategy())
        self.register(MD5Strategy())

    def register(self, strategy: HashStrategy) -> None:
        """
        Register a hash strategy.

        Args:
            strategy: HashStrategy implementation
        """
        self._strategies[strategy.algorithm_name] = strategy

    def get(self, algorithm: str) -> HashStrategy | None:
        """
        Get strategy by algorithm name.

        Args:
            algorithm: Algorithm name (e.g., 'blake3', 'sha256')

        Returns:
            HashStrategy or None if not found
        """
        return self._strategies.get(algorithm)

    def create_hasher(self, algorithm: str) -> Any:
        """
        Create a hasher for the given algorithm.

        Args:
            algorithm: Algorithm name

        Returns:
            Hasher instance

        Raises:
            ValueError: If algorithm not registered
        """
        strategy = self.get(algorithm)
        if strategy is None:
            raise ValueError(f"Unknown hash algorithm: {algorithm}")
        return strategy.create_hasher()

    def compute_hash(self, algorithm: str, data: bytes) -> str:
        """
        Compute hash of data using the specified algorithm.

        Args:
            algorithm: Algorithm name
            data: Data to hash

        Returns:
            Hex-encoded hash digest
        """
        strategy = self.get(algorithm)
        if strategy is None:
            raise ValueError(f"Unknown hash algorithm: {algorithm}")

        hasher = strategy.create_hasher()
        strategy.update(hasher, data)
        return strategy.hexdigest(hasher)

    @property
    def available_algorithms(self) -> list[str]:
        """List available algorithm names."""
        return list(self._strategies.keys())

    def __contains__(self, algorithm: str) -> bool:
        """Check if algorithm is registered."""
        return algorithm in self._strategies
