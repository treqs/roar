"""Application orchestration for `roar get` workflows."""

from __future__ import annotations

from ...application.git import build_roar_git_tag_name, create_roar_git_tag, resolve_git_state
from ...core.bootstrap import bootstrap
from ...core.logging import get_logger
from ...db.context import create_database_context
from ...integrations.download import parse_source, resolve_download_backend
from ...services.get.service import GetService
from .requests import GetRequest, GetResponse


def get_artifacts(request: GetRequest) -> GetResponse:
    """Execute the `roar get` application workflow."""
    bootstrap(request.roar_dir)
    logger = get_logger()

    parsed_source = parse_source(request.source)
    backend = resolve_download_backend(request.source)
    repo_root = request.repo_root or request.cwd
    is_prefix = request.source.rstrip("/") != request.source or parsed_source.is_prefix

    git_commit = None
    if not request.dry_run:
        try:
            git_commit = resolve_git_state(repo_root).commit
            logger.debug("Git commit: %s", git_commit)
        except Exception as exc:
            logger.debug("Git operation failed (non-fatal for get): %s", exc)

    with create_database_context(request.roar_dir) as db_ctx:
        if not request.dry_run:
            active_session = db_ctx.sessions.get_active()
            if not active_session:
                raise ValueError("No active session. Run 'roar reset' or 'roar run' first.")

        service = GetService(
            db_context=db_ctx,
            backend=backend,
            source=parsed_source,
            repo_root=repo_root,
        )
        result = service.get(
            destination=request.destination,
            message=request.message,
            expected_hash=request.expected_hash,
            dry_run=request.dry_run,
            force=request.force,
            git_commit=git_commit,
            is_prefix=is_prefix,
        )

    git_tag_name = None
    warnings: list[str] = []
    if request.tag and git_commit and result.success and not result.dry_run:
        git_tag_name = build_roar_git_tag_name(git_commit)
        try:
            success, tag_error = create_roar_git_tag(repo_root, git_tag_name)
            if not success:
                git_tag_name = None
                if tag_error:
                    warnings.append(f"Could not create git tag: {tag_error}")
        except Exception as exc:
            git_tag_name = None
            warnings.append(f"Could not create git tag: {exc}")

    return GetResponse(result=result, git_tag=git_tag_name, warnings=warnings)

