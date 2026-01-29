"""
Protocol definitions for roar's service interfaces.

These protocols define the contracts that implementations must follow,
enabling dependency inversion and loose coupling throughout the codebase.
"""

from .cloud import ICloudStorageProvider
from .command import CommandContext, CommandResult, ICommand
from .config import IConfigProvider
from .logger import ILogger
from .presenter import IPresenter
from .repositories import (
    ArtifactRepository,
    CollectionRepository,
    HashCacheRepository,
    JobRepository,
    SessionRepository,
)
from .services import (
    HashingService,
    JobRecordingService,
    LineageService,
    SessionService,
)
from .telemetry import ITelemetryProvider, TelemetryRunInfo
from .vcs import IVCSProvider, VCSInfo

__all__ = [
    # Repository protocols
    "ArtifactRepository",
    "CollectionRepository",
    "CommandContext",
    "CommandResult",
    "HashCacheRepository",
    # Service protocols
    "HashingService",
    # Integration protocols
    "ICloudStorageProvider",
    # Command protocols
    "ICommand",
    "IConfigProvider",
    "ILogger",
    "IPresenter",
    "ITelemetryProvider",
    "IVCSProvider",
    "JobRecordingService",
    "JobRepository",
    "LineageService",
    "SessionRepository",
    "SessionService",
    "TelemetryRunInfo",
    "VCSInfo",
]
