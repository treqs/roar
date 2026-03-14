"""Application entrypoints for publish workflows."""

from __future__ import annotations

from ...core.bootstrap import bootstrap
from ...core.logging import get_logger
from ...db.context import create_database_context
from ...integrations.glaas import get_glaas_url
from ...integrations.storage import resolve_publish_storage_backend
from ..git import finalize_put_git, finalize_register_git, prepare_put_git
from .collection import collect_register_lineage
from .put_execution import PutService
from .put_preparation import prepare_put_execution
from .register_execution import RegisterService
from .register_preparation import prepare_register_execution
from .requests import (
    PutRequest,
    RegisterLineageRequest,
)
from .results import (
    PutResponse,
    RegisterLineageResponse,
)
from .runtime import build_publish_runtime
from .targets import resolve_register_lineage_target


def register_lineage_target(request: RegisterLineageRequest) -> RegisterLineageResponse:
    """Run the `roar register` application workflow."""
    logger = get_logger()
    runtime = build_publish_runtime(glaas_url=get_glaas_url())
    service = RegisterService(
        glaas_client=runtime.glaas_client,
        coordinator=runtime.registration_coordinator,
    )
    resolved_target = resolve_register_lineage_target(
        request.target,
        cwd=request.cwd,
        roar_dir=request.roar_dir,
    )
    collected_lineage, error = collect_register_lineage(
        target=resolved_target,
        roar_dir=request.roar_dir,
        cwd=request.cwd,
        lineage_collector=runtime.lineage_collector,
        session_service=runtime.session_service,
        logger=logger,
    )
    if collected_lineage is None:
        return RegisterLineageResponse(success=False, error=error)

    try:
        prepared = prepare_register_execution(
            runtime=runtime,
            roar_dir=request.roar_dir,
            cwd=request.cwd,
            session_id=collected_lineage.session_id,
            dry_run=request.dry_run,
            session_hash_override=collected_lineage.session_hash_override,
            logger=logger,
        )
    except ValueError as exc:
        return RegisterLineageResponse(
            success=False,
            artifact_hash=collected_lineage.artifact_hash,
            error=str(exc),
        )

    result = service.register_prepared_lineage(
        lineage=collected_lineage.lineage,
        roar_dir=request.roar_dir,
        artifact_hash=collected_lineage.artifact_hash,
        dry_run=request.dry_run,
        as_blake3=request.as_blake3,
        skip_confirmation=request.skip_confirmation,
        confirm_callback=request.confirm_callback,
        prepared=prepared,
    )

    finalize_register_git(
        result_success=result.success,
        dry_run=request.dry_run,
        git_tag_name=prepared.git_tag_name,
        git_tag_repo_root=prepared.git_tag_repo_root,
        logger=logger,
    )

    return RegisterLineageResponse(
        success=result.success,
        session_hash=result.session_hash,
        artifact_hash=result.artifact_hash,
        jobs_registered=result.jobs_registered,
        artifacts_registered=result.artifacts_registered,
        links_created=result.links_created,
        error=result.error,
        secrets_detected=list(result.secrets_detected),
        secrets_redacted=result.secrets_redacted,
        aborted_by_user=result.aborted_by_user,
    )


def put_artifacts(request: PutRequest) -> PutResponse:
    """Run the `roar put` application workflow."""
    bootstrap(request.roar_dir)
    logger = get_logger()

    backend = resolve_publish_storage_backend(request.destination)

    repo_root = request.repo_root or request.cwd
    git_state = prepare_put_git(
        repo_root=repo_root,
        dry_run=request.dry_run,
        no_tag=request.no_tag,
        logger=logger,
    )
    warnings = list(git_state.warnings)

    with create_database_context(request.roar_dir) as db_ctx:
        runtime = build_publish_runtime(glaas_url=get_glaas_url())
        service = PutService(
            db_context=db_ctx,
            backend=backend,
            destination=request.destination,
            repo_root=repo_root,
            roar_dir=request.roar_dir,
            lineage_collector=runtime.lineage_collector,
            registration_coordinator=runtime.registration_coordinator,
        )

        prepared = prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=request.roar_dir,
            repo_root=repo_root,
            sources=request.sources,
            destination=request.destination,
            git_commit=git_state.git_commit,
            logger=logger,
        )

        result = service.put_prepared(
            prepared=prepared,
            sources=request.sources,
            message=request.message,
            dry_run=request.dry_run,
            git_commit=git_state.git_commit,
            git_tag=git_state.expected_tag,
        )

        created_git_tag, git_tag_warnings = finalize_put_git(
            result_success=result.success,
            result_dry_run=result.dry_run,
            no_tag=request.no_tag,
            git_commit=git_state.git_commit,
            expected_tag=git_state.expected_tag,
            git_state=git_state.git_state,
            repo_root=repo_root,
            logger=logger,
        )
        warnings.extend(git_tag_warnings)

    return PutResponse(
        success=result.success,
        destination=request.destination,
        job_id=result.job_id,
        job_uid=result.job_uid,
        session_hash=result.session_hash,
        session_url=result.session_url,
        dry_run=result.dry_run,
        uploaded_files=result.uploaded_files,
        would_upload=result.would_upload,
        composites_registered=result.composites_registered,
        git_tag=created_git_tag,
        warnings=warnings,
        error=result.error,
    )
