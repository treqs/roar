"""
Validation utilities for GLaaS API data.

Provides centralized validation to ensure roar never sends placeholder values
like "unknown" to GLaaS, which would corrupt lineage data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Values that indicate missing/placeholder data and should never be sent to GLaaS
FORBIDDEN_PLACEHOLDER_VALUES: frozenset[str | None] = frozenset({"unknown", "Unknown", "", None})

# Valid source_type values for artifacts (None means local/lineage artifacts)
VALID_SOURCE_TYPES: frozenset[str | None] = frozenset({"s3", "gs", "https", None})


@dataclass
class ValidationResult:
    """Result of a validation check."""

    valid: bool
    errors: list[str]

    @classmethod
    def success(cls) -> ValidationResult:
        """Create a successful validation result."""
        return cls(valid=True, errors=[])

    @classmethod
    def failure(cls, *errors: str) -> ValidationResult:
        """Create a failed validation result."""
        return cls(valid=False, errors=list(errors))

    def __bool__(self) -> bool:
        """Allow using ValidationResult in boolean context."""
        return self.valid


def _is_placeholder(value: Any) -> bool:
    """Check if a value is a forbidden placeholder."""
    return value in FORBIDDEN_PLACEHOLDER_VALUES


# Shown when a run has no git context at all — the signature of a run
# captured outside a git repository. One clear, actionable line beats three
# cryptic per-field "X is required" errors.
_NO_GIT_CONTEXT_MESSAGE = (
    "no git context captured — this run was not inside a git repository. "
    "Local runs are still captured; registering to glaas.ai needs a commit to "
    "anchor reproducible lineage, so re-run inside a git repo (with your code "
    "committed) to register it."
)


def _git_context_errors(fields: list[tuple[str, Any, str]]) -> list[str]:
    """Return git-context validation errors for ``fields``.

    ``fields`` is a list of ``(label, value, hint)`` tuples. When *every*
    field is a placeholder — the signature of a run captured outside a git
    repository — return a single explanatory error instead of one cryptic
    line per field. Otherwise return a granular ``<label> is required<hint>``
    line for each placeholder field, preserving useful diagnostics for
    partial cases like a detached HEAD.
    """
    missing = [(label, hint) for (label, value, hint) in fields if _is_placeholder(value)]
    if not missing:
        return []
    if len(missing) == len(fields):
        return [_NO_GIT_CONTEXT_MESSAGE]
    return [f"{label} is required{hint}" for (label, hint) in missing]


def validate_session_registration(
    session_hash: str | None,
    git_repo: str | None,
    git_commit: str | None,
    git_branch: str | None,
) -> ValidationResult:
    """
    Validate data for GLaaS session registration.

    All git context fields are required and must not be placeholders.

    Args:
        session_hash: The computed session hash
        git_repo: Git repository URL
        git_commit: Git commit SHA
        git_branch: Git branch name

    Returns:
        ValidationResult indicating if data is valid for registration
    """
    errors = []

    if _is_placeholder(session_hash):
        errors.append("session_hash is required")

    errors.extend(
        _git_context_errors(
            [
                ("git_repo", git_repo, " (not in a git repository?)"),
                ("git_commit", git_commit, " (no commits yet?)"),
                ("git_branch", git_branch, " (detached HEAD?)"),
            ]
        )
    )

    if errors:
        return ValidationResult.failure(*errors)
    return ValidationResult.success()


def validate_job_registration(
    command: str | None,
    timestamp: float | None,
    session_hash: str | None,
    job_uid: str | None,
    git_commit: str | None,
    git_branch: str | None,
    job_type: str | None,
    step_number: int | None,
) -> ValidationResult:
    """
    Validate data for GLaaS job registration.

    Args:
        command: Command that was executed
        timestamp: Unix timestamp of job start
        session_hash: Session this job belongs to
        job_uid: Unique job identifier
        git_commit: Git commit SHA
        git_branch: Git branch name
        job_type: Type of job
        step_number: Step number in the session

    Returns:
        ValidationResult indicating if data is valid for registration
    """
    errors = []

    if _is_placeholder(command):
        errors.append("command is required")

    ts_valid, ts_error = validate_timestamp(timestamp)
    if not ts_valid and ts_error:
        errors.append(ts_error)

    if _is_placeholder(session_hash):
        errors.append("session_hash is required")

    if _is_placeholder(job_uid):
        errors.append("job_uid is required")

    errors.extend(
        _git_context_errors(
            [
                ("git_commit", git_commit, ""),
                ("git_branch", git_branch, ""),
            ]
        )
    )

    # job_type: None = normal run, "build" = build step (no validation needed)

    step_valid, step_error = validate_step_number(step_number)
    if not step_valid and step_error:
        errors.append(step_error)

    if errors:
        return ValidationResult.failure(*errors)
    return ValidationResult.success()


def validate_artifact_registration(
    hashes: list[dict[str, str]] | None,
    size: int | None,
    source_type: str | None,
    session_hash: str | None,
) -> ValidationResult:
    """
    Validate data for GLaaS artifact registration.

    Args:
        hashes: List of hash entries [{algorithm, digest}, ...]
        size: File size in bytes
        source_type: Type of artifact source ('s3', 'gs', 'https', or None for local)
        session_hash: Session this artifact belongs to

    Returns:
        ValidationResult indicating if data is valid for registration
    """
    errors = []

    if not hashes or len(hashes) == 0:
        errors.append("at least one hash is required")
    else:
        for i, h in enumerate(hashes):
            if not h.get("algorithm"):
                errors.append(f"hashes[{i}].algorithm is required")
            if not h.get("digest"):
                errors.append(f"hashes[{i}].digest is required")

    if size is None or size < 0:
        errors.append("size is required and must be non-negative")

    # source_type must be one of the valid values (None is allowed for local artifacts)
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        errors.append(f"source_type must be 's3', 'gs', 'https', or None, got '{source_type}'")

    if _is_placeholder(session_hash):
        errors.append("session_hash is required")

    if errors:
        return ValidationResult.failure(*errors)
    return ValidationResult.success()


def validate_step_number(step_number: int | None) -> tuple[bool, str | None]:
    """
    Validate a step number.

    Step numbers must be >= 1 (0 is not a valid step).

    Args:
        step_number: The step number to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if step_number is None:
        return False, "step_number is required"
    if step_number < 1:
        return False, f"step_number must be >= 1, got {step_number}"
    return True, None


def validate_timestamp(timestamp: float | None) -> tuple[bool, str | None]:
    """
    Validate a Unix timestamp.

    Timestamps must be positive (0.0 indicates missing data).

    Args:
        timestamp: Unix timestamp to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if timestamp is None:
        return False, "timestamp is required"
    if timestamp <= 0.0:
        return False, f"timestamp must be positive, got {timestamp}"
    return True, None
