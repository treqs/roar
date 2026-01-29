"""
Hash algorithm strategies and registry.

This module implements the Strategy pattern for hash algorithms,
enabling new algorithms to be added without modifying existing code
(Open/Closed Principle).
"""

from .registry import HashAlgorithmRegistry
from .strategies import (
    Blake3Strategy,
    HashStrategy,
    MD5Strategy,
    SHA256Strategy,
    SHA512Strategy,
)

__all__ = [
    "Blake3Strategy",
    "HashAlgorithmRegistry",
    "HashStrategy",
    "MD5Strategy",
    "SHA256Strategy",
    "SHA512Strategy",
]
