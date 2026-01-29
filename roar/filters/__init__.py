"""Filters for classifying and transforming ptrace data."""

from .files import FileClassifier
from .omit import OmitFilter

__all__ = [
    "FileClassifier",
    "OmitFilter",
    "filter_reads",
    "filter_writes",
    "is_noise_read",
    "is_noise_write",
]

# -----------------------------------------------------------------------------
# Simple noise filtering for provenance tracking
# -----------------------------------------------------------------------------

# System paths to filter from reads
_READ_NOISE_PREFIXES = (
    "/sys/",
    "/etc/",
    "/sbin/",
    "/proc/",
    "/dev/",
    "/usr/",
    "/opt/",
    "/lib/",
    "/lib64/",
)

# Torch/triton cache patterns
_TORCH_CACHE_PREFIXES = (
    "/tmp/torchinductor_",
    "/tmp/torch_",
    "/tmp/triton",
)

# Paths to filter from writes
_WRITE_NOISE_PREFIXES = (
    "/dev/",
    "/proc/",
    "/sys/",
    "/dev/shm/",
    "/usr/local/",
    "/usr/lib/",
    "/usr/share/",
    "/opt/",
    "/etc/",
    "/lib/",
    "/lib64/",
    "/tmp/",
)


def is_noise_read(path: str) -> bool:
    """Check if path should be filtered from tracked reads."""
    if path.startswith(_READ_NOISE_PREFIXES):
        return True
    if path.startswith(_TORCH_CACHE_PREFIXES):
        return True
    if "site-packages" in path:
        return True
    return bool(path.endswith(".pyc"))


def is_noise_write(path: str) -> bool:
    """Check if path should be filtered from tracked writes."""
    if path.startswith(_WRITE_NOISE_PREFIXES):
        return True
    if "/.roar/" in path or path.startswith(".roar/"):
        return True
    return bool(path.endswith(".pyc"))


def filter_reads(paths: list) -> list:
    """Filter a list of read paths, removing noise."""
    return [p for p in paths if not is_noise_read(p)]


def filter_writes(paths: list) -> list:
    """Filter a list of written paths, removing noise."""
    return [p for p in paths if not is_noise_write(p)]
