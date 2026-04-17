"""
Session registration service.

Consolidates session hash computation and GLaaS registration logic
from put.py and coordinator.py.
"""

from functools import cached_property

from ....core.interfaces.logger import ILogger
from ....core.interfaces.registration import (
    GitContext,
    ISessionRegistrar,
    SessionRegistrationResult,
)
from ....core.logging import get_logger
from ....core.session_hash import compute_local_session_hash
from ....core.validation import validate_session_registration
from ..client import GlaasClient


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
            logger: Logger instance. If None, uses the configured process logger.
        """
        self._client = client

        self._logger = logger or get_logger()

    @cached_property
    def client(self) -> GlaasClient:
        """Get or create GLaaS client."""
        return self._client or GlaasClient()

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
        session_hash = compute_local_session_hash(
            roar_dir=roar_dir,
            session_id=session_id,
            fallback_suffix=fallback_suffix,
        )
        self._logger.debug(
            "Computed session hash: %s for session_id=%s",
            session_hash[:12],
            session_id if session_id is not None else fallback_suffix or "external",
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
            created=result.get("created") if result else None,
        )

    def create_registration_session(
        self,
        client_session_id: str | None = None,
    ) -> SessionRegistrationResult:
        """Create or resume a durable remote registration session."""
        result, error = self.client.create_registration_session(client_session_id=client_session_id)
        if error:
            self._logger.warning("Registration session creation failed: %s", error)
            return SessionRegistrationResult(success=False, session_hash="", error=error)

        registration_session_id = result.get("registration_session_id") if result else None
        if not isinstance(registration_session_id, str) or not registration_session_id:
            error_msg = "registration session response did not include registration_session_id"
            self._logger.warning("Registration session creation failed: %s", error_msg)
            return SessionRegistrationResult(success=False, session_hash="", error=error_msg)

        self._logger.debug(
            "Registration session ready: %s (created=%s, status=%s)",
            registration_session_id,
            result.get("created") if result else None,
            result.get("status") if result else None,
        )
        return SessionRegistrationResult(
            success=True,
            session_hash="",
            registration_session_id=registration_session_id,
            created=result.get("created") if result else None,
            status=result.get("status") if result else None,
        )

    def finalize_registration_session(
        self,
        registration_session_id: str,
        git_context: GitContext,
    ) -> SessionRegistrationResult:
        """Finalize a remote registration session into an immutable lineage hash."""
        validation = validate_session_registration(
            session_hash="pending-registration-session-finalize",
            git_repo=git_context.repo,
            git_commit=git_context.commit,
            git_branch=git_context.branch,
        )
        if not validation:
            error_msg = "; ".join(validation.errors)
            self._logger.warning("Registration session finalize validation failed: %s", error_msg)
            return SessionRegistrationResult(success=False, session_hash="", error=error_msg)

        result, error = self.client.finalize_registration_session(
            registration_session_id=registration_session_id,
            git_repo=git_context.repo or "",
            git_commit=git_context.commit or "",
            git_branch=git_context.branch or "",
        )
        if error:
            self._logger.warning("Registration session finalize failed: %s", error)
            return SessionRegistrationResult(success=False, session_hash="", error=error)

        session_hash = result.get("hash") if result else None
        if not isinstance(session_hash, str) or not session_hash:
            error_msg = "registration session finalize response did not include hash"
            self._logger.warning("Registration session finalize failed: %s", error_msg)
            return SessionRegistrationResult(success=False, session_hash="", error=error_msg)

        session_url = result.get("url") if result else None
        self._logger.debug(
            "Registration session finalized successfully: %s -> %s",
            registration_session_id,
            session_hash[:12],
        )
        return SessionRegistrationResult(
            success=True,
            session_hash=session_hash,
            session_url=session_url if isinstance(session_url, str) else None,
            registration_session_id=registration_session_id,
            created=result.get("created") if result else None,
            status=result.get("status") if result else None,
        )
