"""
Storage backends for roar put command.

Provides abstraction over various cloud storage providers.
"""

from .base import Destination, StorageBackend, parse_destination
from .memory import MemoryBackend
from .noop import NoOpBackend, should_skip_upload

__all__ = [
    "Destination",
    "MemoryBackend",
    "NoOpBackend",
    "StorageBackend",
    "parse_destination",
    "should_skip_upload",
]

# Lazy import S3Backend to avoid ImportError when boto3 not installed
try:
    from .s3 import S3Backend  # noqa: F401

    __all__.append("S3Backend")
except ImportError:
    pass

# Lazy import GCSBackend to avoid ImportError when google-cloud-storage not installed
try:
    from .gcs import GCSBackend  # noqa: F401

    __all__.append("GCSBackend")
except ImportError:
    pass
