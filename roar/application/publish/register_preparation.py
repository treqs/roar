"""Application-owned preparation for register workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...config import config_get
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from .git import build_publish_tag_name, ensure_clean_publish_repo, resolve_publish_git_context
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


def prepare_register_execution(
    *,
    runtime: PublishRuntime,
    roar_dir: Path,
    cwd: Path,
    session_id: int | None,
    dry_run: bool,
    session_hash_override: str | None,
    logger: ILogger,
) -> PreparedRegisterExecution:
    """Resolve the local context needed to execute a register workflow."""
    git_context = resolve_publish_git_context(cwd, logger=logger)

    git_tag_name: str | None = None
    git_tag_repo_root: Path | None = None

    tagging_enabled = config_get("registration.tagging.enabled")
    if tagging_enabled is None:
        tagging_enabled = True

    if not dry_run and tagging_enabled and git_context.commit:
        git_state = ensure_clean_publish_repo(
            cwd,
            error_message="Cannot register with uncommitted changes. Commit your changes first.",
        )
        git_tag_name = build_publish_tag_name(git_context.commit, short=True)
        git_tag_repo_root = git_state.repo_root

    publish_session = prepare_publish_session(
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        roar_dir=roar_dir,
        session_id=session_id,
        git_context=git_context,
        logger=logger,
        register_with_glaas=not dry_run,
        configured_error="GLaaS not configured. Run 'roar config set glaas.url <url>' first.",
        session_hash_override=session_hash_override,
    )

    return PreparedRegisterExecution(
        git_context=git_context,
        session_id=session_id,
        session_hash=publish_session.session_hash,
        session_url=publish_session.session_url,
        git_tag_name=git_tag_name,
        git_tag_repo_root=git_tag_repo_root,
    )
