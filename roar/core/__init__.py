"""
Core infrastructure for roar's dependency injection and plugin architecture.

This module provides:
- ServiceContainer: DI container using dependency-injector
- Plugin registry with auto-discovery
- Application bootstrap for initialization
- Protocol definitions for all service interfaces
- Custom exception hierarchy
"""

from .bootstrap import bootstrap, is_initialized, reset
from .container import ServiceContainer, get_container, resolve, try_resolve
from .exceptions import (
    CloudDownloadError,
    CloudUploadError,
    ConfigFileError,
    ConfigValidationError,
    DatabaseConnectionError,
    GlaasAPIError,
    GlaasConnectionError,
    PluginLoadError,
    RoarCloudError,
    RoarConfigError,
    RoarDatabaseError,
    RoarException,
    RoarExecutionError,
    RoarNetworkError,
    RoarPluginError,
    RoarValidationError,
    TracerNotFoundError,
)
from .registry import discover_commands, discover_plugins

__all__ = [
    "CloudDownloadError",
    "CloudUploadError",
    "ConfigFileError",
    "ConfigValidationError",
    "DatabaseConnectionError",
    "GlaasAPIError",
    "GlaasConnectionError",
    "PluginLoadError",
    "RoarCloudError",
    "RoarConfigError",
    "RoarDatabaseError",
    "RoarException",
    "RoarExecutionError",
    "RoarNetworkError",
    "RoarPluginError",
    "RoarValidationError",
    "ServiceContainer",
    "TracerNotFoundError",
    "bootstrap",
    "discover_commands",
    "discover_plugins",
    "get_container",
    "is_initialized",
    "reset",
    "resolve",
    "try_resolve",
]
