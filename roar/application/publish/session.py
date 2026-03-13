"""Shared publish-session orchestration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext, SessionRegistrationResult
from ...glaas_client import GlaasClient


class PublishSessionService(Protocol):
    """Minimal session-service contract for publish workflows."""

    def compute_session_hash(self, *, roar_dir: str, session_id: int | None) -> str:
        """Compute the local session hash."""

    def register(
        self,
        session_hash: str,
        git_context: GitContext,
    ) -> SessionRegistrationResult:
        """Register a session with GLaaS."""


@dataclass(frozen=True)
class PreparedPublishSession:
    """Shared publish-session preparation result."""

    session_hash: str
    session_url: str | None = None


def prepare_publish_session(
    *,
    glaas_client: GlaasClient,
    session_service: PublishSessionService,
    roar_dir: Path,
    session_id: int | None,
    git_context: GitContext,
    logger: ILogger,
    register_with_glaas: bool,
    configured_error: str | None = None,
    session_hash_override: str | None = None,
) -> PreparedPublishSession:
    """Compute and optionally register the publish session."""
    if session_hash_override:
        session_hash = session_hash_override
    else:
        if session_id is None:
            raise ValueError("Cannot compute a session hash without a local session id.")
        session_hash = session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session_id,
        )

    logger.debug("Session hash: %s", session_hash[:12])

    if not register_with_glaas:
        return PreparedPublishSession(session_hash=session_hash)

    if configured_error is not None and not glaas_client.is_configured():
        raise ValueError(configured_error)

    logger.debug("Running GLaaS health check")
    try:
        glaas_client.health_check()
    except Exception as exc:
        logger.debug("GLaaS health check failed: %s", exc)
        raise ValueError(f"GLaaS health check failed: {exc}") from exc

    logger.debug("Registering session with GLaaS")
    session_result = session_service.register(session_hash, git_context)
    if not session_result.success:
        logger.debug("Session registration failed: %s", session_result.error)
        raise ValueError(f"Session registration failed: {session_result.error}")

    logger.debug("Session registered: url=%s", session_result.session_url)
    return PreparedPublishSession(
        session_hash=session_hash,
        session_url=session_result.session_url,
    )
