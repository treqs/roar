"""Application entrypoints for publish workflows."""

from __future__ import annotations

from ...core.bootstrap import bootstrap
from ...core.logging import get_logger
from ...db.context import create_database_context
from ...glaas_client import get_glaas_url
from ...integrations.storage import (
    MemoryBackend,
    NoOpBackend,
    load_backend_class,
    parse_destination,
    resolve_backend_for_scheme,
    should_skip_upload,
)
from ...services.put import PutService
from ...services.registration.register_service import RegisterResult, RegisterService
from .collection import collect_register_lineage
from .git import build_publish_tag_name, create_publish_git_tag, ensure_clean_publish_repo
from .put_preparation import prepare_put_execution
from .register_preparation import prepare_register_execution
from .requests import (
    PutRequest,
    PutResponse,
    RegisterLineageRequest,
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
        return RegisterLineageResponse(result=RegisterResult(success=False, error=error))

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
            result=RegisterResult(
                success=False,
                artifact_hash=collected_lineage.artifact_hash,
                error=str(exc),
            )
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

    if (
        result.success
        and not request.dry_run
        and prepared.git_tag_name
        and prepared.git_tag_repo_root is not None
    ):
        try:
            success, tag_error = create_publish_git_tag(
                prepared.git_tag_repo_root,
                prepared.git_tag_name,
            )
            if not success and tag_error:
                logger.debug("Failed to create git tag: %s", tag_error)
        except Exception as exc:
            logger.debug("Failed to create git tag: %s", exc)

    return RegisterLineageResponse(result=result)


def put_artifacts(request: PutRequest) -> PutResponse:
    """Run the `roar put` application workflow."""
    bootstrap(request.roar_dir)
    logger = get_logger()

    parse_destination(request.destination)
    backend = _get_backend(request.destination)

    git_commit: str | None = None
    expected_tag: str | None = None
    warnings: list[str] = []
    repo_root = request.repo_root or request.cwd

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

        git_state = None
        if not request.dry_run:
            try:
                git_state = ensure_clean_publish_repo(
                    repo_root,
                    error_message=(
                        "Repository has uncommitted changes.\n"
                        "Please commit your changes before running 'roar put'.\n"
                        "This ensures artifacts can be traced to a specific commit."
                    ),
                )
                git_commit = git_state.commit
                if not request.no_tag:
                    expected_tag = build_publish_tag_name(git_commit)
            except ValueError:
                raise
            except Exception as exc:
                logger.debug("Git operation failed during put preflight: %s", exc)
                warnings.append(f"Git operation failed: {exc}")

        prepared = prepare_put_execution(
            db_ctx=db_ctx,
            runtime=runtime,
            roar_dir=request.roar_dir,
            repo_root=repo_root,
            sources=request.sources,
            destination=request.destination,
            git_commit=git_commit,
            logger=logger,
        )

        result = service.put_prepared(
            prepared=prepared,
            sources=request.sources,
            message=request.message,
            dry_run=request.dry_run,
            git_commit=git_commit,
            git_tag=expected_tag,
        )

        created_git_tag: str | None = None
        if result.success and not result.dry_run and not request.no_tag and git_commit:
            try:
                tag_name = expected_tag or build_publish_tag_name(git_commit)
                success, tag_error = create_publish_git_tag(
                    git_state.repo_root if git_state is not None else repo_root,
                    tag_name,
                )
                if success:
                    created_git_tag = tag_name
                elif tag_error:
                    warnings.append(f"Could not create git tag: {tag_error}")
            except Exception as exc:
                logger.debug("Git tag creation failed: %s", exc)
                warnings.append(f"Could not create git tag: {exc}")

    return PutResponse(
        result=result,
        git_tag=created_git_tag,
        warnings=warnings,
    )


def _get_backend(destination: str):
    """Resolve the storage backend for a publish destination."""
    parsed = parse_destination(destination)

    if should_skip_upload():
        return NoOpBackend(
            bucket=parsed.bucket,
            prefix=parsed.prefix,
            scheme=parsed.scheme,
        )

    builders = {
        "s3": lambda: load_backend_class(
            "roar.integrations.storage.s3",
            "S3Backend",
            "S3 backend requires boto3. Install with: pip install boto3",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "gs": lambda: load_backend_class(
            "roar.integrations.storage.gcs",
            "GCSBackend",
            "GCS backend requires google-cloud-storage. Install with: pip install google-cloud-storage",
        )(bucket=parsed.bucket, prefix=parsed.prefix),
        "memory": lambda: MemoryBackend(bucket=parsed.bucket, prefix=parsed.prefix),
    }

    return resolve_backend_for_scheme(
        parsed.scheme,
        builders,
        f"Unsupported destination scheme: {parsed.scheme}",
    )
