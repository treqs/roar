class ExecutionSetupError(RuntimeError):
    """Raised when a backend cannot start a host-side execution path."""


__all__ = ["ExecutionSetupError"]
