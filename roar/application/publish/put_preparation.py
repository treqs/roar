"""Application-owned preparation for put workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...glaas_client import GlaasClient
from .git import resolve_publish_git_context
from .runtime import PublishRuntime
from .session import prepare_publish_session

if TYPE_CHECKING:
    from ...services.put.resolver import ResolvedSource


@dataclass(frozen=True)
class PreparedPutExecution:
    """Application-prepared context for a put execution."""

    glaas_client: GlaasClient
    session_id: int
    session_hash: str
    session_url: str | None
    git_context: GitContext
    resolved_sources: list[ResolvedSource]
    destination_type: str
    composite_source_type: str | None


def prepare_put_execution(
    *,
    db_ctx,
    runtime: PublishRuntime,
    roar_dir: Path,
    repo_root: Path,
    sources: list[str],
    destination: str,
    git_commit: str | None,
    logger: ILogger,
) -> PreparedPutExecution:
    """Resolve the local context needed to execute a put workflow."""
    from ...services.put.resolver import SourceResolver

    active_session = db_ctx.sessions.get_active()
    if active_session is None:
        raise ValueError("No active session")

    session_id = int(active_session["id"])
    git_context = resolve_publish_git_context(
        repo_root,
        logger=logger,
        git_commit=git_commit,
    )
    publish_session = prepare_publish_session(
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        roar_dir=roar_dir,
        session_id=session_id,
        git_context=git_context,
        logger=logger,
        register_with_glaas=True,
    )

    resolver = SourceResolver(
        repo_root=repo_root,
        session_repo=db_ctx.sessions,
        job_repo=db_ctx.jobs,
    )
    resolved_sources = resolver.resolve(sources)

    destination_type = _destination_type(destination)
    composite_source_type = destination_type if destination_type in {"s3", "gs", "https"} else None

    return PreparedPutExecution(
        glaas_client=runtime.glaas_client,
        session_id=session_id,
        session_hash=publish_session.session_hash,
        session_url=publish_session.session_url,
        git_context=git_context,
        resolved_sources=resolved_sources,
        destination_type=destination_type,
        composite_source_type=composite_source_type,
    )


def _destination_type(destination: str) -> str:
    parsed = urlparse(destination)
    return parsed.scheme or "local"
