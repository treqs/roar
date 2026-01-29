"""
Custom exception hierarchy for roar.

Provides a structured exception hierarchy to replace silent error suppression
with explicit, typed exceptions that can be handled appropriately.
"""

from __future__ import annotations


class RoarException(Exception):
    """
    Base exception for all roar errors.

    Attributes:
        message: Human-readable error description
        context: Additional debugging context (file paths, URLs, etc.)
        exit_code: Suggested exit code for CLI (default: 1)
        recoverable: Whether retry/recovery may be possible
    """

    exit_code: int = 1
    recoverable: bool = True

    def __init__(
        self,
        message: str,
        *,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.message = message
        self.context = context or {}
        if cause is not None:
            self.__cause__ = cause
        super().__init__(message)

    def __str__(self) -> str:
        if self.context:
            ctx_str = ", ".join(f"{k}={v!r}" for k, v in self.context.items())
            return f"{self.message} ({ctx_str})"
        return self.message


# =============================================================================
# Configuration Errors
# =============================================================================


class RoarConfigError(RoarException):
    """Base class for configuration-related errors."""

    pass


class ConfigFileError(RoarConfigError):
    """
    Error reading or parsing a configuration file.

    Raised for TOML parsing errors, file not found, permission errors, etc.
    """

    def __init__(
        self,
        message: str,
        *,
        file_path: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if file_path:
            ctx["file_path"] = file_path
        super().__init__(message, context=ctx, cause=cause)


class ConfigValidationError(RoarConfigError, ValueError):
    """
    Invalid or missing configuration value.

    Inherits from ValueError for backward compatibility with code
    that catches ValueError for validation errors.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        value: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if key:
            ctx["key"] = key
        if value is not None:
            ctx["value"] = value
        super().__init__(message, context=ctx, cause=cause)


# =============================================================================
# Database Errors
# =============================================================================


class RoarDatabaseError(RoarException):
    """Base class for database-related errors."""

    pass


class DatabaseConnectionError(RoarDatabaseError):
    """
    Error connecting to or initializing the database.

    Raised when the database is not connected, cannot be opened,
    or schema initialization fails.
    """

    def __init__(
        self,
        message: str,
        *,
        db_path: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if db_path:
            ctx["db_path"] = db_path
        super().__init__(message, context=ctx, cause=cause)


class DatabaseQueryError(RoarDatabaseError):
    """
    Error executing a database query.

    Raised for SQL errors, constraint violations, etc.
    """

    pass


# =============================================================================
# Plugin Errors
# =============================================================================


class RoarPluginError(RoarException):
    """Base class for plugin-related errors."""

    pass


class PluginLoadError(RoarPluginError):
    """
    Error loading or instantiating a plugin.

    Raised when a plugin module cannot be imported or
    a plugin class cannot be instantiated.
    """

    def __init__(
        self,
        message: str,
        *,
        plugin_name: str | None = None,
        plugin_type: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if plugin_name:
            ctx["plugin_name"] = plugin_name
        if plugin_type:
            ctx["plugin_type"] = plugin_type
        super().__init__(message, context=ctx, cause=cause)


class PluginNotFoundError(RoarPluginError):
    """
    Requested plugin not found.

    Raised when a plugin identified by name or type is not registered.
    """

    def __init__(
        self,
        message: str,
        *,
        plugin_name: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if plugin_name:
            ctx["plugin_name"] = plugin_name
        super().__init__(message, context=ctx, cause=cause)


# =============================================================================
# Cloud Errors
# =============================================================================


class RoarCloudError(RoarException):
    """Base class for cloud storage-related errors."""

    pass


class CloudAuthenticationError(RoarCloudError):
    """
    Cloud authentication or authorization failure.

    Raised when credentials are invalid, expired, or insufficient.
    """

    recoverable: bool = False


class CloudUploadError(RoarCloudError):
    """
    Error uploading to cloud storage.

    Raised when an upload operation fails.
    """

    def __init__(
        self,
        message: str,
        *,
        source_path: str | None = None,
        dest_url: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if source_path:
            ctx["source_path"] = source_path
        if dest_url:
            ctx["dest_url"] = dest_url
        super().__init__(message, context=ctx, cause=cause)


class CloudDownloadError(RoarCloudError):
    """
    Error downloading from cloud storage.

    Raised when a download operation fails.
    """

    def __init__(
        self,
        message: str,
        *,
        source_url: str | None = None,
        dest_path: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if source_url:
            ctx["source_url"] = source_url
        if dest_path:
            ctx["dest_path"] = dest_path
        super().__init__(message, context=ctx, cause=cause)


class CloudResourceNotFoundError(RoarCloudError):
    """
    Cloud resource not found.

    Raised when a requested bucket, object, or path does not exist.
    """

    def __init__(
        self,
        message: str,
        *,
        resource_url: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if resource_url:
            ctx["resource_url"] = resource_url
        super().__init__(message, context=ctx, cause=cause)


# =============================================================================
# Network Errors (GLaaS)
# =============================================================================


class RoarNetworkError(RoarException):
    """Base class for network-related errors."""

    pass


class GlaasConnectionError(RoarNetworkError):
    """
    Error connecting to GLaaS server.

    Raised for connection timeouts, DNS failures, SSL errors, etc.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if url:
            ctx["url"] = url
        super().__init__(message, context=ctx, cause=cause)


class GlaasAPIError(RoarNetworkError):
    """
    GLaaS API returned an error response.

    Includes HTTP status code for programmatic handling.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if status_code:
            ctx["status_code"] = status_code
        if url:
            ctx["url"] = url
        super().__init__(message, context=ctx, cause=cause)
        self.status_code = status_code


class GlaasTimeoutError(RoarNetworkError):
    """
    GLaaS request timed out.

    Raised when a request exceeds the configured timeout.
    """

    recoverable: bool = True

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        timeout: float | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if url:
            ctx["url"] = url
        if timeout is not None:
            ctx["timeout"] = timeout
        super().__init__(message, context=ctx, cause=cause)


# =============================================================================
# Execution Errors
# =============================================================================


class RoarExecutionError(RoarException):
    """Base class for execution-related errors."""

    pass


class TracerNotFoundError(RoarExecutionError):
    """
    The roar-tracer binary was not found.

    Raised when the tracer binary cannot be located in expected paths.
    """

    exit_code: int = 1
    recoverable: bool = False


class TracerStartupError(RoarExecutionError):
    """
    The tracer failed to start or initialize.

    Raised when the tracer process fails during startup.
    """

    def __init__(
        self,
        message: str,
        *,
        tracer_path: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if tracer_path:
            ctx["tracer_path"] = tracer_path
        super().__init__(message, context=ctx, cause=cause)


class ProcessExecutionError(RoarExecutionError):
    """
    Error during process execution.

    Raised for errors during traced process execution.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        command: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if exit_code is not None:
            ctx["exit_code"] = exit_code
        if command:
            ctx["command"] = command
        super().__init__(message, context=ctx, cause=cause)


# =============================================================================
# Provenance Errors
# =============================================================================


class RoarProvenanceError(RoarException):
    """
    Error during provenance collection.

    Provenance collection is best-effort, so these errors are typically
    logged but don't fail the operation.
    """

    recoverable: bool = True


# =============================================================================
# Validation Errors
# =============================================================================


class RoarValidationError(RoarException, ValueError):
    """
    Base class for input validation errors.

    Inherits from ValueError for backward compatibility.
    """

    pass


class RegistrationValidationError(RoarValidationError):
    """
    Raised when required data for GLaaS API calls is missing or invalid.

    This indicates that registration cannot proceed because the data would corrupt
    lineage records (e.g., placeholder values like "unknown").
    """

    def __init__(
        self,
        message: str,
        *,
        validation_errors: list[str] | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if validation_errors:
            ctx["validation_errors"] = validation_errors
        super().__init__(message, context=ctx, cause=cause)


# Backwards compatibility alias
SyncValidationError = RegistrationValidationError


class GitContextMissingError(RegistrationValidationError):
    """
    Raised when registration requires git context but it's unavailable.

    This typically happens when:
    - Not in a git repository
    - Repository has no commits
    - HEAD is detached with no branch

    Registration should be skipped (with warning) rather than failing the operation.
    """

    def __init__(
        self,
        message: str = "Git context unavailable for registration",
        *,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, context=context, cause=cause)


class InvalidArgumentError(RoarValidationError):
    """
    Invalid command-line argument or function parameter.

    Raised when user input fails validation.
    """

    def __init__(
        self,
        message: str,
        *,
        argument: str | None = None,
        value: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if argument:
            ctx["argument"] = argument
        if value is not None:
            ctx["value"] = value
        super().__init__(message, context=ctx, cause=cause)


class GitStateError(RoarValidationError):
    """
    Invalid or unexpected git repository state.

    Raised when git operations fail due to repository state
    (dirty worktree, detached HEAD, etc.).
    """

    def __init__(
        self,
        message: str,
        *,
        repo_path: str | None = None,
        context: dict | None = None,
        cause: Exception | None = None,
    ) -> None:
        ctx = context or {}
        if repo_path:
            ctx["repo_path"] = repo_path
        super().__init__(message, context=ctx, cause=cause)
