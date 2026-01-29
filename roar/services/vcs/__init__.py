"""
VCS services for roar.

This package provides services for version control system operations,
extracted from command files to follow SOLID principles.

Services:
- GitAccessService: Check git push access permissions
"""

from .git_access import GitAccessService

__all__ = [
    "GitAccessService",
]
