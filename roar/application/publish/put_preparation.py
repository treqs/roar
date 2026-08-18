"""Application-owned preparation for put workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...integrations.glaas import GlaasClient
from ..git import resolve_roar_git_context
from .datasets import (
    detect_additional_publish_composite_roots,
    infer_publish_dataset_identifiers,
)
from .remote_registry import coerce_remote_registry
from .runtime import PublishRuntime
from .session import prepare_publish_session

if TYPE_CHECKING:
    from .source_resolution import ResolvedSource


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
    registration_session_id: str | None = None
    registration_session_mode: str | None = None
    registration_session_status: str | None = None
    dataset_identifiers: list[dict[str, Any]] = field(default_factory=list)
    additional_composite_roots: dict[Path, list[ResolvedSource]] = field(default_factory=dict)


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
    from .source_resolution import SourceResolver

    active_session = db_ctx.sessions.get_active()
    if active_session is None:
        raise ValueError("No active session")

    session_id = int(active_session["id"])
    from ...integrations.config import config_get

    git_context = resolve_roar_git_context(
        repo_root,
        logger=logger,
        git_commit=git_commit,
        configured_remote=config_get("git.remote"),
    )
    runtime_dict = getattr(runtime, "__dict__", {})
    remote_registry = coerce_remote_registry(
        remote_registry=runtime_dict.get("remote_registry"),
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        registration_coordinator=runtime_dict.get("registration_coordinator"),
    )
    publish_session = prepare_publish_session(
        remote_registry=remote_registry,
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
    dataset_identifiers = infer_publish_dataset_identifiers(
        repo_root=repo_root,
        source_specs=sources,
        resolved_sources=resolved_sources,
    )
    additional_composite_roots = detect_additional_publish_composite_roots(
        resolved_sources=resolved_sources,
    )

    destination_type = _destination_type(destination)
    composite_source_type = (
        destination_type if destination_type in {"s3", "gs", "https", "hf"} else None
    )

    return PreparedPutExecution(
        glaas_client=runtime.glaas_client,
        session_id=session_id,
        session_hash=publish_session.session_hash,
        session_url=publish_session.session_url,
        git_context=git_context,
        registration_session_id=publish_session.registration_session_id,
        registration_session_mode=publish_session.registration_session_mode,
        registration_session_status=(
            publish_session.registration_session_status
            if isinstance(publish_session.registration_session_status, str)
            else None
        ),
        resolved_sources=resolved_sources,
        destination_type=destination_type,
        composite_source_type=composite_source_type,
        dataset_identifiers=dataset_identifiers,
        additional_composite_roots=additional_composite_roots,
    )


def _destination_type(destination: str) -> str:
    parsed = urlparse(destination)
    return parsed.scheme or "local"
