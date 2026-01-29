"""
Step reference parser for DAG node references.

Parses @N and @BN style references used to identify DAG steps:
- @N: Run step N (e.g., @1, @2, @3)
- @BN: Build step N (e.g., @B1, @B2)

This module extracts the duplicated step reference parsing logic
from show.py and dag.py into a single, reusable implementation.
"""

from __future__ import annotations

from dataclasses import dataclass


class StepReferenceError(ValueError):
    """Error parsing a step reference."""

    pass


@dataclass(frozen=True)
class StepReference:
    """Parsed step reference.

    Attributes:
        step_number: The numeric step number (1-indexed)
        is_build: Whether this is a build step (@BN) vs run step (@N)
        original: The original reference string
    """

    step_number: int
    is_build: bool
    original: str

    @property
    def prefix(self) -> str:
        """Get the step prefix (@B or @)."""
        return "@B" if self.is_build else "@"

    @property
    def formatted(self) -> str:
        """Get the formatted step reference."""
        return f"{self.prefix}{self.step_number}"

    @property
    def job_type(self) -> str | None:
        """Get the job type for database queries.

        Returns:
            "build" for build steps, None for run steps
            (None matches the default job type in queries)
        """
        return "build" if self.is_build else None


def parse_step_reference(ref: str) -> StepReference:
    """Parse a step reference string.

    Args:
        ref: Step reference string (e.g., "@1", "@B2", "1", "B2")

    Returns:
        Parsed StepReference

    Raises:
        StepReferenceError: If the reference is invalid

    Examples:
        >>> parse_step_reference("@1")
        StepReference(step_number=1, is_build=False, original='@1')

        >>> parse_step_reference("@B2")
        StepReference(step_number=2, is_build=True, original='@B2')

        >>> parse_step_reference("3")
        StepReference(step_number=3, is_build=False, original='3')

        >>> parse_step_reference("B1")
        StepReference(step_number=1, is_build=True, original='B1')
    """
    original = ref
    working = ref

    # Strip leading @ if present
    if working.startswith("@"):
        working = working[1:]

    if not working:
        raise StepReferenceError(f"Invalid step reference '{original}': empty after removing @")

    # Check for build step prefix
    is_build = False
    if working.upper().startswith("B"):
        is_build = True
        working = working[1:]

    if not working:
        raise StepReferenceError(f"Invalid step reference '{original}': no step number after B")

    # Parse the step number
    if not working.isdigit():
        raise StepReferenceError(
            f"Invalid step reference '{original}': "
            f"expected number, got '{working}'. Use @N or @BN format."
        )

    step_number = int(working)

    if step_number < 1:
        raise StepReferenceError(
            f"Invalid step reference '{original}': step number must be positive, got {step_number}"
        )

    return StepReference(
        step_number=step_number,
        is_build=is_build,
        original=original,
    )


def is_step_reference(s: str) -> bool:
    """Check if a string looks like a step reference.

    This is a quick check without full parsing, useful for
    dispatching logic that needs to distinguish step references
    from other identifiers (hashes, UIDs, etc.).

    Args:
        s: String to check

    Returns:
        True if the string starts with @ (step reference syntax)

    Examples:
        >>> is_step_reference("@1")
        True
        >>> is_step_reference("@B2")
        True
        >>> is_step_reference("abc123")
        False
    """
    return s.startswith("@")


def format_step_not_found_error(ref: StepReference) -> str:
    """Format an error message for a step that wasn't found.

    Args:
        ref: The step reference that wasn't found

    Returns:
        User-friendly error message
    """
    return f"No {ref.prefix}{ref.step_number} in DAG."
