"""
Protocol definitions for roar's service interfaces.

These protocols define the contracts that implementations must follow,
enabling dependency inversion and loose coupling throughout the codebase.
"""

from .config import IConfigProvider
from .lineage import ILineageCollector, LineageData
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
from .telemetry import ITelemetryProvider
from .vcs import IVCSProvider

__all__ = [
    "ArtifactRepository",
    "CollectionRepository",
    "HashCacheRepository",
    "HashingService",
    "IConfigProvider",
    "ILineageCollector",
    "ILogger",
    "IPresenter",
    "ITelemetryProvider",
    "IVCSProvider",
    "JobRecordingService",
    "JobRepository",
    "LineageData",
    "LineageService",
    "SessionRepository",
    "SessionService",
]
