"""
Roar plugin architecture.

This package contains provider implementations for external services:
- cloud: Cloud storage providers (S3, GCS)

Each provider type follows the Open/Closed Principle: new providers
can be added without modifying existing code by registering them
with the service container.
"""

from . import cloud

__all__ = ["cloud"]
