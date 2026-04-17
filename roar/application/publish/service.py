"""Application entrypoints for publish workflows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.logging import get_logger
from .requests import (
    PutRequest,
    RegisterLineageRequest,
)
from .results import (
    PutCompositeRegistration,
    PutDryRunItem,
    PutResponse,
    PutUploadedFile,
    RegisterLineageResponse,
)

if TYPE_CHECKING:
    from ...db.query_context import QueryDatabaseContext


def bootstrap(roar_dir: Path) -> None:
    """Load bootstrap dependencies only when the put workflow runs."""
    from ...core.bootstrap import bootstrap as _bootstrap

    _bootstrap(roar_dir)


def create_database_context(roar_dir: Path) -> Any:
    """Load database context factory lazily for publish workflows."""
    from ...db.context import create_database_context as _create_database_context

    return _create_database_context(roar_dir)


def create_query_database_context(roar_dir: Path) -> Any:
    """Load the lightweight query DB context only for read-only publish flows."""
    from ...db.query_context import create_query_database_context as _create_query_database_context

    return _create_query_database_context(roar_dir)


def get_glaas_url() -> str | None:
    """Load GLaaS config lookup lazily."""
    from ...integrations.glaas import get_glaas_url as _get_glaas_url

    return _get_glaas_url()


def resolve_publish_storage_backend(destination: str) -> Any:
    """Resolve storage backends only for non-dry-run put operations."""
    from ...integrations.storage import (
        resolve_publish_storage_backend as _resolve_publish_storage_backend,
    )

    return _resolve_publish_storage_backend(destination)


def prepare_put_git(*args: Any, **kwargs: Any) -> Any:
    """Load git helpers only for put workflows."""
    from ..git import prepare_put_git as _prepare_put_git

    return _prepare_put_git(*args, **kwargs)


def finalize_put_git(*args: Any, **kwargs: Any) -> Any:
    """Load git helpers only for put workflows."""
    from ..git import finalize_put_git as _finalize_put_git

    return _finalize_put_git(*args, **kwargs)


def finalize_register_git(*args: Any, **kwargs: Any) -> Any:
    """Load git helpers only for register workflows."""
    from ..git import finalize_register_git as _finalize_register_git

    return _finalize_register_git(*args, **kwargs)


def collect_register_lineage(*args: Any, **kwargs: Any) -> Any:
    """Load lineage collection only when register runs."""
    from .collection import collect_register_lineage as _collect_register_lineage

    return _collect_register_lineage(*args, **kwargs)


def prepare_put_execution(*args: Any, **kwargs: Any) -> Any:
    """Load put preparation only for non-dry-run put operations."""
    from .put_preparation import prepare_put_execution as _prepare_put_execution

    return _prepare_put_execution(*args, **kwargs)


def prepare_register_execution(*args: Any, **kwargs: Any) -> Any:
    """Load register preparation only when register runs."""
    from .register_preparation import (
        prepare_register_execution as _prepare_register_execution,
    )

    return _prepare_register_execution(*args, **kwargs)


def PutService(*args: Any, **kwargs: Any) -> Any:
    """Construct the put service only when the put execution path is used."""
    from .put_execution import PutService as _PutService

    return _PutService(*args, **kwargs)


def RegisterService(*args: Any, **kwargs: Any) -> Any:
    """Construct the register service only when the register path is used."""
    from .register_execution import RegisterService as _RegisterService

    return _RegisterService(*args, **kwargs)


def build_publish_runtime(
    *,
    glaas_url: str | None = None,
    start_dir: str | None = None,
    allow_public_without_binding: bool = False,
) -> Any:
    """Load publish runtime assembly only when publish workflows execute."""
    from .runtime import build_publish_runtime as _build_publish_runtime

    return _build_publish_runtime(
        glaas_url=glaas_url,
        start_dir=start_dir,
        allow_public_without_binding=allow_public_without_binding,
    )


def resolve_register_lineage_target(*args: Any, **kwargs: Any) -> Any:
    """Load register target resolution only when register runs."""
    from .targets import (
        resolve_register_lineage_target as _resolve_register_lineage_target,
    )

    return _resolve_register_lineage_target(*args, **kwargs)


@dataclass(frozen=True)
class _PutPlanResult:
    """Lightweight local plan result for `roar put --dry-run`."""

    success: bool
    dry_run: bool
    would_upload: list[PutDryRunItem] = field(default_factory=list)
    uploaded_files: list[PutUploadedFile] = field(default_factory=list)
    composites_registered: list[PutCompositeRegistration] = field(default_factory=list)
    job_id: int | None = None
    job_uid: str | None = None
    session_hash: str | None = None
    session_url: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class _RegisterPreviewRuntime:
    """Minimal runtime surface for local `roar register --dry-run` flows."""

    glaas_client: Any
    session_service: Any
    lineage_collector: Any


@dataclass(frozen=True)
class _PreparedRegisterPreviewExecution:
    """Local preview-only register preparation result."""

    git_context: Any
    session_id: int | None
    session_hash: str
    session_url: str | None
    git_tag_name: str | None = None
    git_tag_repo_root: Path | None = None


def build_register_preview_runtime(
    *,
    start_dir: str | None = None,
    allow_public_without_binding: bool = False,
) -> Any:
    """Build only the dependencies needed for local register preview flows."""
    from ...integrations.glaas.client import GlaasClient
    from ...integrations.glaas.registration.session import SessionRegistrationService
    from ...publish_auth import PublishAuthContext
    from .lineage import LineageCollector

    publish_auth = None
    if not allow_public_without_binding:
        publish_auth = PublishAuthContext(
            access_token=None,
            scope_request=None,
            auth_provider=None,
            user_sub=None,
            db_user_id=None,
        )

    glaas_client = GlaasClient(
        None,
        start_dir=start_dir,
        publish_auth=publish_auth,
        allow_public_without_binding=allow_public_without_binding,
    )
    return _RegisterPreviewRuntime(
        glaas_client=glaas_client,
        session_service=SessionRegistrationService(glaas_client),
        lineage_collector=LineageCollector(),
    )


def prepare_register_preview_execution(
    *,
    runtime: Any,
    roar_dir: Path,
    cwd: Path,
    session_id: int | None,
    session_hash_override: str | None,
    logger: Any,
    lineage: Any | None = None,
) -> Any:
    """Prepare local register preview state without importing full git workflow helpers."""
    from ...core.canonical_session import compute_canonical_session_hash
    from ...publish_auth import resolve_publish_creator_identity
    from .session import build_canonical_session_payload

    git_context = _resolve_register_preview_git_context(path=cwd, logger=logger)
    if session_hash_override:
        session_hash = session_hash_override
    elif lineage is not None:
        creator_identity = resolve_publish_creator_identity(runtime.glaas_client.publish_auth)
        session_hash = compute_canonical_session_hash(
            build_canonical_session_payload(
                lineage=lineage,
                git_context=git_context,
                creator_identity=creator_identity,
            )
        )
    else:
        if session_id is None:
            raise ValueError("Cannot compute a session hash without a local session id.")
        session_hash = runtime.session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session_id,
        )

    logger.debug("Session hash: %s", session_hash[:12])
    return _PreparedRegisterPreviewExecution(
        git_context=git_context,
        session_id=session_id,
        session_hash=session_hash,
        session_url=None,
    )


def preview_register_lineage(
    *,
    lineage: Any,
    artifact_hash: str,
    prepared: Any,
    cwd: Path,
    skip_confirmation: bool,
    confirm_callback: Any,
) -> RegisterLineageResponse:
    """Build a local register preview result without importing real registration machinery."""
    from ...filters.omit import OmitFilter
    from ...integrations.config.raw import get_raw_registration_omit_config
    from .register_preview_jobs import (
        estimate_links,
        normalize_jobs_for_registration,
        order_jobs_for_registration,
    )
    from .secrets import detect_lineage_secrets, filter_lineage_secrets

    omit_filter = None
    omit_config = get_raw_registration_omit_config(start_dir=str(cwd))
    if omit_config.get("enabled", True):
        omit_filter = OmitFilter(omit_config)

    detected_secrets: list[str] = []
    if omit_filter is not None:
        detected_secrets = detect_lineage_secrets(
            lineage=lineage,
            git_context=prepared.git_context,
            omit_filter=omit_filter,
        )
        if detected_secrets and not skip_confirmation:
            if confirm_callback is None:
                return RegisterLineageResponse(
                    success=False,
                    session_hash=prepared.session_hash,
                    artifact_hash=artifact_hash,
                    error="Secrets detected in data. Use --yes to proceed with redacted data.",
                    secrets_detected=detected_secrets,
                    aborted_by_user=True,
                )

            if not confirm_callback(detected_secrets):
                return RegisterLineageResponse(
                    success=False,
                    session_hash=prepared.session_hash,
                    artifact_hash=artifact_hash,
                    error="Registration aborted by user.",
                    secrets_detected=detected_secrets,
                    aborted_by_user=True,
                )

    if detected_secrets or (omit_filter is not None and omit_filter.enabled):
        lineage = filter_lineage_secrets(
            lineage=lineage,
            omit_filter=omit_filter,
        )

    registration_jobs = order_jobs_for_registration(normalize_jobs_for_registration(lineage.jobs))
    return RegisterLineageResponse(
        success=True,
        session_hash=prepared.session_hash,
        artifact_hash=artifact_hash,
        jobs_registered=len(registration_jobs),
        artifacts_registered=len(lineage.artifacts),
        links_created=estimate_links(registration_jobs),
        secrets_detected=detected_secrets,
        secrets_redacted=bool(detected_secrets),
    )


def _resolve_register_preview_git_context(*, path: Path, logger: Any) -> Any:
    """Resolve git context for local preview flows without importing git provider models."""
    from ...core.interfaces.registration import GitContext

    repo_root = _run_git(path, "rev-parse", "--show-toplevel")
    if not repo_root:
        logger.debug("No git repository found for register preview at %s", path)
        return GitContext(repo=None, commit=None, branch=None)

    repo_root_path = Path(repo_root)
    commit = _run_git(repo_root_path, "rev-parse", "HEAD")
    branch = _run_git(repo_root_path, "rev-parse", "--abbrev-ref", "HEAD")
    remote = _run_git(repo_root_path, "remote", "get-url", "origin")
    repo = remote or repo_root_path.resolve().as_uri()
    if remote is None:
        logger.debug(
            "No git remote configured for %s; using local repository URI %s",
            repo_root_path,
            repo,
        )

    return GitContext(repo=repo, commit=commit, branch=branch)


def _run_git(path: Path, *args: str) -> str | None:
    """Run a git command and return stripped stdout on success."""
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=path,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def register_lineage_target(request: RegisterLineageRequest) -> RegisterLineageResponse:
    """Run the `roar register` application workflow."""
    from ...publish_auth import PublishAuthError

    if not request.dry_run:
        bootstrap(request.roar_dir)
    logger = get_logger()

    try:
        resolved_target = resolve_register_lineage_target(
            request.target,
            cwd=request.cwd,
            roar_dir=request.roar_dir,
        )
        runtime = (
            build_register_preview_runtime(
                start_dir=str(request.cwd),
                allow_public_without_binding=request.public,
            )
            if request.dry_run
            else build_publish_runtime(
                glaas_url=get_glaas_url(),
                start_dir=str(request.cwd),
                allow_public_without_binding=request.public,
            )
        )
        collected_lineage, error = collect_register_lineage(
            target=resolved_target,
            roar_dir=request.roar_dir,
            cwd=request.cwd,
            lineage_collector=runtime.lineage_collector,
            session_service=runtime.session_service,
            logger=logger,
            dry_run=request.dry_run,
        )
        if collected_lineage is None:
            return RegisterLineageResponse(success=False, error=error)

        try:
            if request.dry_run:
                prepared = prepare_register_preview_execution(
                    runtime=runtime,
                    roar_dir=request.roar_dir,
                    cwd=request.cwd,
                    session_id=collected_lineage.session_id,
                    session_hash_override=collected_lineage.session_hash_override,
                    logger=logger,
                    lineage=collected_lineage.lineage,
                )
            else:
                prepared = prepare_register_execution(
                    runtime=runtime,
                    roar_dir=request.roar_dir,
                    cwd=request.cwd,
                    session_id=collected_lineage.session_id,
                    dry_run=False,
                    session_hash_override=collected_lineage.session_hash_override,
                    logger=logger,
                    lineage=collected_lineage.lineage,
                )
        except ValueError as exc:
            return RegisterLineageResponse(
                success=False,
                artifact_hash=collected_lineage.artifact_hash,
                error=str(exc),
            )

        if request.dry_run:
            return preview_register_lineage(
                lineage=collected_lineage.lineage,
                artifact_hash=collected_lineage.artifact_hash,
                prepared=prepared,
                cwd=request.cwd,
                skip_confirmation=request.skip_confirmation,
                confirm_callback=request.confirm_callback,
            )

        service = RegisterService(
            glaas_client=runtime.glaas_client,
            coordinator=runtime.registration_coordinator,
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
    except PublishAuthError as exc:
        return RegisterLineageResponse(success=False, error=str(exc))


def put_artifacts(request: PutRequest) -> PutResponse:
    """Run the `roar put` application workflow."""
    if not request.dry_run:
        bootstrap(request.roar_dir)
    logger = get_logger()

    repo_root = request.repo_root or request.cwd
    if request.dry_run:
        git_state = None
        git_commit = None
        expected_tag = None
        warnings: list[str] = []
    else:
        git_state = prepare_put_git(
            repo_root=repo_root,
            dry_run=False,
            no_tag=request.no_tag,
            logger=logger,
        )
        git_commit = git_state.git_commit
        expected_tag = git_state.expected_tag
        warnings = list(git_state.warnings)

    if request.dry_run:
        with create_query_database_context(request.roar_dir) as db_ctx:
            result = _plan_put_dry_run(
                db_ctx=db_ctx,
                repo_root=repo_root,
                sources=request.sources,
            )
    else:
        with create_database_context(request.roar_dir) as db_ctx:
            backend = resolve_publish_storage_backend(request.destination)
            runtime = build_publish_runtime(
                glaas_url=get_glaas_url(),
                start_dir=str(repo_root),
                allow_public_without_binding=request.public,
            )
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

    if request.dry_run:
        created_git_tag = None
    else:
        created_git_tag, git_tag_warnings = finalize_put_git(
            result_success=result.success,
            result_dry_run=result.dry_run,
            no_tag=request.no_tag,
            git_commit=git_commit,
            expected_tag=expected_tag,
            git_state=git_state.git_state if git_state is not None else None,
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


def _plan_put_dry_run(
    *,
    db_ctx: QueryDatabaseContext,
    repo_root: Path,
    sources: list[str],
) -> _PutPlanResult:
    """Resolve the local source plan for `roar put --dry-run`."""
    from .source_resolution import SourceResolver

    active_session = db_ctx.sessions.get_active()
    if active_session is None:
        raise ValueError("No active session")

    resolver = SourceResolver(
        repo_root=repo_root,
        session_repo=db_ctx.sessions,
        job_repo=db_ctx.jobs,
    )
    resolved_sources = resolver.resolve(sources)
    would_upload = [
        PutDryRunItem(path=str(source.path), exists=source.exists) for source in resolved_sources
    ]
    return _PutPlanResult(
        success=True,
        dry_run=True,
        would_upload=would_upload,
    )
