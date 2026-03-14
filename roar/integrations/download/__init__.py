"""Download integration adapters for `roar get` workflows."""

from .base import DownloadBackend, Source, parse_source
from .get import resolve_download_backend
from .noop import NoOpDownloadBackend, should_skip_download

__all__ = [
    "DownloadBackend",
    "NoOpDownloadBackend",
    "Source",
    "parse_source",
    "resolve_download_backend",
    "should_skip_download",
]

try:
    from .s3 import S3DownloadBackend  # noqa: F401

    __all__.append("S3DownloadBackend")
except ImportError:
    pass

try:
    from .gcs import GCSDownloadBackend  # noqa: F401

    __all__.append("GCSDownloadBackend")
except ImportError:
    pass
