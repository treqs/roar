"""
Source resolver for roar put command.

Resolves various source specifications (file paths, directories,
job references, artifact hashes) to concrete file paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ...core.logging import get_logger

if TYPE_CHECKING:
    from typing import Any


class SessionRepository(Protocol):
    """Protocol for session repository dependency."""

    def get_active(self) -> dict[str, Any] | None: ...
    def get_steps(self, session_id: int) -> list[dict[str, Any]]: ...
    def get_step_by_number(self, session_id: int, step_number: int) -> dict[str, Any] | None: ...


class JobRepository(Protocol):
    """Protocol for job repository dependency."""

    def get_outputs(self, job_id: int) -> list[dict[str, Any]]: ...


@dataclass
class ResolvedSource:
    """A resolved source ready for upload."""

    path: Path
    exists: bool
    relative_key: str = ""  # Key for upload (relative to source dir or just filename)


# Pattern for job reference: @N where N is a positive integer
JOB_REF_PATTERN = re.compile(r"^@(\d+)$")


class SourceResolver:
    """
    Resolves source specifications to file paths.

    Supports:
    - File paths
    - Directory paths (recursive)
    - Job references (@N)
    - Artifact hashes
    - Default to session outputs (when no sources specified)
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        session_repo: SessionRepository | None = None,
        job_repo: JobRepository | None = None,
    ):
        """
        Initialize resolver.

        Args:
            repo_root: Repository root directory for relative path resolution.
            session_repo: Session repository for job reference resolution.
            job_repo: Job repository for fetching job outputs.
        """
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._session_repo = session_repo
        self._job_repo = job_repo
        self._logger = get_logger()

    def resolve(self, sources: list[str]) -> list[ResolvedSource]:
        """
        Resolve source specifications to file paths.

        If sources is empty, defaults to all outputs from the active session.

        Args:
            sources: List of source specifications (paths, @N refs, hashes).
                     If empty, defaults to all session outputs.

        Returns:
            List of ResolvedSource objects.

        Raises:
            FileNotFoundError: If a specified file does not exist.
            ValueError: If no active session when sources is empty.
        """
        # Default to all session outputs if no sources specified
        if not sources:
            self._logger.debug("No sources specified — resolving session outputs")
            resolved = self._resolve_session_outputs()
            self._logger.debug("Resolved %d session output(s)", len(resolved))
            return resolved

        self._logger.debug("Resolving %d source spec(s): %s", len(sources), sources)
        results: list[ResolvedSource] = []

        for source in sources:
            resolved = self._resolve_single(source)
            self._logger.debug(
                "Source %r -> %d file(s)",
                source,
                len(resolved),
            )
            results.extend(resolved)

        self._logger.debug("Total resolved: %d file(s)", len(results))
        return results

    def _resolve_single(self, source: str) -> list[ResolvedSource]:
        """
        Resolve a single source specification.

        Args:
            source: Source specification.

        Returns:
            List of ResolvedSource objects (may be multiple for directories).

        Raises:
            FileNotFoundError: If a specified file does not exist.
            ValueError: If a job reference is invalid.
        """
        # Check for job reference pattern (@N)
        job_match = JOB_REF_PATTERN.match(source)
        if job_match:
            step_number = int(job_match.group(1))
            self._logger.debug("Resolving job reference @%d", step_number)
            return self._resolve_job_reference(step_number)

        path = Path(source)

        # Make absolute if relative
        if not path.is_absolute():
            path = self._repo_root / path

        path = path.resolve()

        if not path.exists():
            self._logger.debug("Source not found: %s (resolved to %s)", source, path)
            raise FileNotFoundError(f"Source not found: {source}")

        # If it's a directory, walk it recursively
        if path.is_dir():
            self._logger.debug("Resolving directory: %s", path)
            return self._resolve_directory(path)

        self._logger.debug("Resolved file: %s", path)
        return [ResolvedSource(path=path, exists=True, relative_key=path.name)]

    def _resolve_directory(self, directory: Path) -> list[ResolvedSource]:
        """
        Recursively resolve all files in a directory.

        Args:
            directory: Directory path to walk.

        Returns:
            List of ResolvedSource objects for all files in the directory.
            Each file's relative_key is its path relative to the source directory.
        """
        results: list[ResolvedSource] = []

        for item in directory.rglob("*"):
            if item.is_file():
                relative_key = str(item.relative_to(directory))
                results.append(ResolvedSource(path=item, exists=True, relative_key=relative_key))

        return results

    def _resolve_session_outputs(self) -> list[ResolvedSource]:
        """
        Resolve all outputs from the active session.

        Returns:
            List of ResolvedSource objects for all session outputs.

        Raises:
            ValueError: If no active session.
        """
        if self._session_repo is None:
            raise ValueError("Session repository required to resolve session outputs")

        active_session = self._session_repo.get_active()
        if active_session is None:
            raise ValueError("No active session")

        session_id = active_session["id"]
        steps = self._session_repo.get_steps(session_id)

        if not steps:
            return []

        if self._job_repo is None:
            raise ValueError("Job repository required to resolve session outputs")

        results: list[ResolvedSource] = []
        for step in steps:
            job_id = step["id"]
            outputs = self._job_repo.get_outputs(job_id)
            for output in outputs:
                path = Path(output["path"])
                results.append(
                    ResolvedSource(path=path, exists=path.exists(), relative_key=path.name)
                )

        return results

    def _resolve_job_reference(self, step_number: int) -> list[ResolvedSource]:
        """
        Resolve a job reference (@N) to output file paths.

        Args:
            step_number: The step number to get outputs from.

        Returns:
            List of ResolvedSource objects for all outputs of the step.

        Raises:
            ValueError: If no active session or step doesn't exist.
        """
        if self._session_repo is None:
            raise ValueError("Session repository required to resolve job references")

        # Get active session
        active_session = self._session_repo.get_active()
        if active_session is None:
            raise ValueError("No active session")

        session_id = active_session["id"]

        # Get the step
        step = self._session_repo.get_step_by_number(session_id, step_number)
        if step is None:
            raise ValueError(f"Step {step_number} not found in session")

        if self._job_repo is None:
            raise ValueError("Job repository required to resolve job references")

        # Get outputs from the job
        job_id = step["id"]
        outputs = self._job_repo.get_outputs(job_id)

        results: list[ResolvedSource] = []
        for output in outputs:
            path = Path(output["path"])
            results.append(ResolvedSource(path=path, exists=path.exists(), relative_key=path.name))

        return results
