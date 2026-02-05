"""
Download backends for roar get command.

Provides abstraction over various cloud storage and HTTP download sources.
"""

from .base import DownloadBackend, Source, parse_source
from .noop import NoOpDownloadBackend, should_skip_download

__all__ = [
    "DownloadBackend",
    "NoOpDownloadBackend",
    "Source",
    "parse_source",
    "should_skip_download",
]

# Lazy import S3DownloadBackend to avoid ImportError when boto3 not installed
try:
    from .s3 import S3DownloadBackend  # noqa: F401

    __all__.append("S3DownloadBackend")
except ImportError:
    pass

# Lazy import GCSDownloadBackend to avoid ImportError when google-cloud-storage not installed
try:
    from .gcs import GCSDownloadBackend  # noqa: F401

    __all__.append("GCSDownloadBackend")
except ImportError:
    pass
