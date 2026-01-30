"""
Session registration service.

Consolidates session hash computation and GLaaS registration logic
from put.py and coordinator.py.
"""

import hashlib
from pathlib import Path

from ...core.di import resolve_or_default
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import (
    GitContext,
    ISessionRegistrar,
    SessionRegistrationResult,
)
from ...core.validation import validate_session_registration
from ...glaas_client import GlaasClient


class SessionRegistrationService(ISessionRegistrar):
    """
    Service for session registration operations.

    Consolidates the duplicated session hash computation and registration
    logic from put.py (lines 172-186) and coordinator.py (lines 300-301).
    """

    def __init__(self, client: GlaasClient | None = None, logger: ILogger | None = None):
        """
        Initialize the session registration service.

        Args:
            client: GLaaS client for server communication. If None, creates one.
            logger: Logger instance. If None, resolves from DI container.
        """
        self._client = client
        from ...services.logging import NullLogger

        self._logger = logger or resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]

    @property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        if self._client is None:
            self._client = GlaasClient()
        return self._client

    def compute_session_hash(
        self,
        roar_dir: str,
        session_id: int | None,
        fallback_suffix: str | None = None,
    ) -> str:
        """
        Compute session hash from roar directory and session ID.

        This consolidates the identical hash computation from:
        - put.py:174-175: session_id_str = f"{roar_dir_abs}:{pipeline['id']}"
        - coordinator.py:300-301: session_id_str = f"{ctx.roar_dir}:{session['id']}"

        Args:
            roar_dir: Path to .roar directory
            session_id: Session ID from database, or None for fallback
            fallback_suffix: Suffix to use if session_id is None (e.g., "put:timestamp")

        Returns:
            SHA256 hash of the session identifier string
        """
        roar_dir_abs = Path(roar_dir)

        if session_id is not None:
            session_id_str = f"{roar_dir_abs}:{session_id}"
        elif fallback_suffix:
            session_id_str = f"{roar_dir_abs}:{fallback_suffix}"
        else:
            # Generate a unique session for external files
            import time

            session_id_str = f"{roar_dir_abs}:external:{time.time()}"

        session_hash = hashlib.sha256(session_id_str.encode()).hexdigest()
        self._logger.debug(
            "Computed session hash: %s from %s",
            session_hash[:12],
            session_id_str[:50],
        )
        return session_hash

    def register(
        self,
        session_hash: str,
        git_context: GitContext,
    ) -> SessionRegistrationResult:
        """
        Register session with GLaaS after validation.

        This consolidates the duplicated validation and registration from:
        - put.py:193-225
        - coordinator.py:306-325

        Args:
            session_hash: Pre-computed session hash
            git_context: Git context (repo, commit, branch)

        Returns:
            SessionRegistrationResult with success status and details
        """
        # Validate session data before registration
        validation = validate_session_registration(
            session_hash=session_hash,
            git_repo=git_context.repo,
            git_commit=git_context.commit,
            git_branch=git_context.branch,
        )
        if not validation:
            error_msg = "; ".join(validation.errors)
            self._logger.warning("Session validation failed: %s", error_msg)
            return SessionRegistrationResult(
                success=False,
                session_hash=session_hash,
                error=error_msg,
            )

        # Register with GLaaS
        result, error = self.client.register_session(
            session_hash=session_hash,
            git_repo=git_context.repo or "",
            git_commit=git_context.commit or "",
            git_branch=git_context.branch or "",
        )

        if error:
            self._logger.warning("Session registration failed: %s", error)
            return SessionRegistrationResult(
                success=False,
                session_hash=session_hash,
                error=error,
            )

        session_url = result.get("url") if result else None
        self._logger.debug(
            "Session registered successfully: %s, url=%s",
            session_hash[:12],
            session_url,
        )

        return SessionRegistrationResult(
            success=True,
            session_hash=session_hash,
            session_url=session_url,
        )
