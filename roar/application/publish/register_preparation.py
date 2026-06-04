"""Application-owned preparation for register workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...publish_auth import resolve_publish_creator_identity
from ..git import build_roar_git_tag_name, ensure_clean_git_repo, resolve_roar_git_context
from .job_preparation import normalize_jobs_for_registration, order_jobs_for_registration
from .remote_registry import coerce_remote_registry
from .runtime import PublishRuntime
from .session import prepare_publish_session


@dataclass(frozen=True)
class PreparedRegisterExecution:
    """Application-prepared context for a register execution."""

    git_context: GitContext
    session_id: int | None
    session_hash: str
    session_url: str | None
    git_tag_name: str | None
    git_tag_repo_root: Path | None
    registration_session_id: str | None = None
    registration_session_mode: str | None = None


def prepare_register_execution(
    *,
    runtime: PublishRuntime,
    roar_dir: Path,
    cwd: Path,
    session_id: int | None,
    dry_run: bool,
    session_hash_override: str | None,
    logger: ILogger,
    lineage: LineageData | None = None,
) -> PreparedRegisterExecution:
    """Resolve the local context needed to execute a register workflow."""
    from ...integrations.config import config_get

    git_context = resolve_roar_git_context(
        cwd, logger=logger, configured_remote=config_get("git.remote")
    )

    git_tag_name: str | None = None
    git_tag_repo_root: Path | None = None

    tagging_enabled = True
    if not dry_run:
        from ...integrations.config import config_get

        tagging_enabled = config_get("registration.tagging.enabled")
        if tagging_enabled is None:
            tagging_enabled = True

    if not dry_run and tagging_enabled and git_context.commit:
        git_state = ensure_clean_git_repo(
            cwd,
            error_message="Cannot register with uncommitted changes. Commit your changes first.",
        )
        git_tag_name = build_roar_git_tag_name(git_context.commit, short=True)
        git_tag_repo_root = git_state.repo_root

    runtime_dict = getattr(runtime, "__dict__", {})
    remote_registry = coerce_remote_registry(
        remote_registry=runtime_dict.get("remote_registry"),
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        registration_coordinator=runtime_dict.get("registration_coordinator"),
    )
    creator_identity = resolve_publish_creator_identity(remote_registry.publish_auth)
    canonical_lineage = None
    if lineage is not None:
        canonical_lineage = LineageData(
            jobs=order_jobs_for_registration(normalize_jobs_for_registration(lineage.jobs)),
            artifacts=list(lineage.artifacts),
            artifact_hashes=set(lineage.artifact_hashes),
            pipeline=dict(lineage.pipeline)
            if isinstance(lineage.pipeline, dict)
            else lineage.pipeline,
        )

    publish_session = prepare_publish_session(
        remote_registry=remote_registry,
        roar_dir=roar_dir,
        session_id=session_id,
        git_context=git_context,
        logger=logger,
        register_with_glaas=not dry_run,
        configured_error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
        session_hash_override=session_hash_override,
        lineage=canonical_lineage,
        creator_identity=creator_identity,
    )

    return PreparedRegisterExecution(
        git_context=git_context,
        session_id=session_id,
        session_hash=publish_session.session_hash,
        session_url=publish_session.session_url,
        git_tag_name=git_tag_name,
        git_tag_repo_root=git_tag_repo_root,
        registration_session_id=publish_session.registration_session_id,
        registration_session_mode=publish_session.registration_session_mode,
    )
