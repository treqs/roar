"""
Version control system provider plugins.

Provides implementations for various VCS backends.
"""

from .base import BaseVCSProvider, VCSInfo
from .git import GitVCSProvider

__all__ = [
    "BaseVCSProvider",
    "GitVCSProvider",
    "VCSInfo",
]
