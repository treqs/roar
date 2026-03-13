"""Cloud/object storage integration adapters for publish and transfer flows."""

from ..resolution import load_backend_class, resolve_backend_for_scheme
from .base import Destination, StorageBackend, parse_destination
from .memory import MemoryBackend
from .noop import NoOpBackend, should_skip_upload
from .publish import resolve_publish_storage_backend

__all__ = [
    "Destination",
    "MemoryBackend",
    "NoOpBackend",
    "StorageBackend",
    "load_backend_class",
    "parse_destination",
    "resolve_backend_for_scheme",
    "resolve_publish_storage_backend",
    "should_skip_upload",
]

try:
    from .s3 import S3Backend  # noqa: F401

    __all__.append("S3Backend")
except ImportError:
    pass

try:
    from .gcs import GCSBackend  # noqa: F401

    __all__.append("GCSBackend")
except ImportError:
    pass
