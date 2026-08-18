"""Application entrypoints for publish workflows."""

from __future__ import annotations

import contextlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
from .targets import ResolvedRegisterTarget

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
    force_anonymous: bool = False,
) -> Any:
    """Load publish runtime assembly only when publish workflows execute."""
    from .runtime import build_publish_runtime as _build_publish_runtime

    return _build_publish_runtime(
        glaas_url=glaas_url,
        start_dir=start_dir,
        allow_public_without_binding=allow_public_without_binding,
        force_anonymous=force_anonymous,
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

    remote_registry: Any
    lineage_collector: Any

    @property
    def glaas_client(self) -> Any:
        return self.remote_registry.client

    @property
    def session_service(self) -> Any:
        return self.remote_registry.session_service


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
    force_anonymous: bool = False,
) -> Any:
    """Build only the dependencies needed for local register preview flows."""
    from ...integrations.glaas.client import GlaasClient
    from ...integrations.glaas.registration.session import SessionRegistrationService
    from ...publish_auth import PublishAuthContext
    from .lineage import LineageCollector
    from .remote_registry import GlaasRemoteRegistryTransport

    publish_auth = None
    if force_anonymous:
        publish_auth = PublishAuthContext(
            access_token=None,
            scope_request=None,
            auth_provider=None,
            user_sub=None,
            db_user_id=None,
            creator_identity=None,
            ssh_auth_available=False,
        )
    elif not allow_public_without_binding:
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
        force_anonymous=force_anonymous,
    )
    session_service = SessionRegistrationService(glaas_client)
    return _RegisterPreviewRuntime(
        remote_registry=GlaasRemoteRegistryTransport(
            client=glaas_client,
            session_service=session_service,
        ),
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
        creator_identity = resolve_publish_creator_identity(runtime.remote_registry.publish_auth)
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
    # Publish-only preview: never record a local ``file://`` URI here. A repo
    # with no shareable remote publishes as "no remote" (matching the real
    # register path, which drops it in `resolve_roar_git_context`) rather than
    # leaking the local filesystem path to GLaaS.
    repo = remote if remote else None
    if remote is None:
        logger.debug("No git remote configured for %s; publishing with no remote", repo_root_path)

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


def _collect_db_job_ids(lineage: Any) -> list[int]:
    """The integer DB job ids of a collected lineage's jobs (skips non-numeric)."""
    job_ids: list[int] = []
    for job in getattr(lineage, "jobs", []) or []:
        raw_id = job.get("id") if isinstance(job, dict) else None
        if isinstance(raw_id, int):
            job_ids.append(raw_id)
        elif isinstance(raw_id, str) and raw_id.isdigit():
            job_ids.append(int(raw_id))
    return job_ids


def _attribute_jobs_to_anchors_for_lineage(*, roar_dir: Path, lineage: Any, logger: Any) -> int:
    """Materialize job -> dataset-anchor edges for the jobs in a collected lineage.

    Opens its own DB context so the persisted edges are visible to a fresh lineage
    collection. Returns the number of edges added.
    """
    from .anchor_attribution import attribute_jobs_to_anchors

    job_ids = _collect_db_job_ids(lineage)
    if not job_ids:
        return 0
    with create_database_context(roar_dir) as attr_db:
        return attribute_jobs_to_anchors(db_ctx=attr_db, job_ids=job_ids, logger=logger)


def _form_lineage_composites_for_lineage(*, roar_dir: Path, lineage: Any, logger: Any) -> int:
    """Form composites from each job's recorded inputs/outputs in a collected lineage.

    Opens its own DB context so the persisted composites + job edges are visible to a
    fresh lineage collection. Returns the number of edges added.
    """
    from .lineage_composite_formation import form_lineage_composites

    job_ids = _collect_db_job_ids(lineage)
    if not job_ids:
        return 0
    with create_database_context(roar_dir) as form_db:
        return form_lineage_composites(db_ctx=form_db, job_ids=job_ids, logger=logger)


def _session_single_commit(*, roar_dir: Path, session_id: Any) -> bool:
    """True iff the session's steps all ran at one git commit.

    Computed from the session's commit span (``git_commit_start`` vs
    ``git_commit_end``) — the SAME formula ``roar reproduce`` uses (see
    ``reproduce/lookup.py``) — so register's receipt and reproduce's preview can
    never disagree on the "single git commit" check. The old proxy (extra job
    tags) read False-positive whenever tagging was skipped (e.g. no remote),
    silently mis-scoring multi-commit lineages as single-commit. Best-effort:
    defaults True (the existing assume-single behavior) on any miss.
    """
    if session_id is None:
        return True
    try:
        with create_query_database_context(roar_dir) as db_ctx:
            session = db_ctx.sessions.get(session_id)
    except Exception:
        return True
    if not session:
        return True
    start = session.get("git_commit_start")
    end = session.get("git_commit_end")
    return not (start and end and start != end)


def _load_remote_job_uid_mapping(*, roar_dir: Path, session_id: Any) -> dict[str, str]:
    """Return the ``{local_job_uid: remote_job_uid}`` map registration persisted.

    GLaaS stores jobs under publication-scoped remote UIDs (see
    ``remote_job_uids.build_remote_publication_job_uid``); registration records the
    local->remote mapping under ``roar.remote_publication.glaas.jobs`` in the session
    metadata. Returns an empty map (callers fall back to the local UID) on any miss.
    """
    if session_id is None:
        return {}
    try:
        with create_database_context(roar_dir) as db:
            session = db.sessions.get(session_id)
    except Exception:
        return {}
    if not session:
        return {}
    raw = session.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    jobs = (((raw.get("roar") or {}).get("remote_publication") or {}).get("glaas") or {}).get(
        "jobs"
    )
    if not isinstance(jobs, dict):
        return {}
    return {str(k): str(v) for k, v in jobs.items() if isinstance(v, str) and v}


def _prepare_view_edges_for_lineage(
    *, roar_dir: Path, lineage: Any, logger: Any
) -> tuple[dict[str, list[Any]], set[str]]:
    """Compute consumes/produces view edges per job and prune the subsumed leaves in-place.

    A job that read part of a dataset gets a ``consumes`` view edge over its inputs; a job
    that wrote one gets a ``produces`` view edge over its outputs. Each edge is a bloom
    over the touched leaves referencing the anchor composite; the subsumed leaf hashes —
    and any anchor linked as a plain input/output — are removed from the bundle so the job
    links the dataset *view* (job -> view -> anchor -> leaves), not the loose leaves or the
    anchor directly. The anchor composite stays in ``lineage.artifacts`` (already
    collected) so it is still registered.

    Returns ``({job_uid: [view_edge, ...]}, composite_leaf_hashes)``: the view edges to
    push after the main registration, and the union of true component/leaf hashes (never
    the anchor's own hash) subsumed across all jobs — a leaf has no session-scoped edge on
    GLaaS, so the caller uses this set to keep leaf label/tag documents out of publish-time
    label sync too.
    """
    from .view_edges import resolve_view_edges_for_job

    jobs = getattr(lineage, "jobs", []) or []
    if not jobs:
        return {}, set()
    view_edges_by_job: dict[str, list[Any]] = {}
    composite_leaf_hashes: set[str] = set()
    with create_database_context(roar_dir) as db:
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_uid = job.get("job_uid")
            if not job_uid:
                continue
            edges: list[Any] = []
            sides: tuple[tuple[Literal["consumes", "produces"], str, str], ...] = (
                ("consumes", "_input_hashes", "_inputs"),
                ("produces", "_output_hashes", "_outputs"),
            )
            for relation, hashes_key, items_key in sides:
                hashes = list(job.get(hashes_key) or [])
                if not hashes:
                    continue
                resolved, prune, leaf_hashes = resolve_view_edges_for_job(
                    db_ctx=db, input_hashes=hashes, relation=relation
                )
                if not resolved:
                    continue
                edges.extend(resolved)
                job[hashes_key] = [h for h in hashes if h not in prune]
                if isinstance(job.get(items_key), list):
                    job[items_key] = [i for i in job[items_key] if i.get("hash") not in prune]
                composite_leaf_hashes |= leaf_hashes
            if edges:
                view_edges_by_job[str(job_uid)] = edges
    return view_edges_by_job, composite_leaf_hashes


def register_lineage_target(request: RegisterLineageRequest) -> RegisterLineageResponse:
    """Run the `roar register` application workflow."""
    from ...publish_auth import PublishAuthError

    if not request.dry_run:
        bootstrap(request.roar_dir)
    logger = get_logger()

    if request.anonymous and not request.public:
        return RegisterLineageResponse(
            success=False,
            error="Anonymous registration requires public visibility.",
        )

    try:
        resolved_target = (
            ResolvedRegisterTarget(kind="active_session", value="")
            if request.target is None
            else resolve_register_lineage_target(
                request.target,
                cwd=request.cwd,
                roar_dir=request.roar_dir,
            )
        )
        runtime_kwargs: dict[str, Any] = {
            "start_dir": str(request.cwd),
            "allow_public_without_binding": request.public,
        }
        if request.anonymous:
            runtime_kwargs["force_anonymous"] = True
        runtime = (
            build_register_preview_runtime(**runtime_kwargs)
            if request.dry_run
            else build_publish_runtime(glaas_url=get_glaas_url(), **runtime_kwargs)
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

        if not request.dry_run:
            try:
                # Roll structured datasets a run produced/consumed (Zarr, LeRobot, ...)
                # up into composites, then attribute jobs to dataset anchors via the get
                # crosswalk. Both mutate the local DB; if either added edges, re-read the
                # lineage so the bundle carries them. Best-effort: never blocks a register.
                formed_composites = _form_lineage_composites_for_lineage(
                    roar_dir=request.roar_dir,
                    lineage=collected_lineage.lineage,
                    logger=logger,
                )
                anchor_added = _attribute_jobs_to_anchors_for_lineage(
                    roar_dir=request.roar_dir,
                    lineage=collected_lineage.lineage,
                    logger=logger,
                )
                if formed_composites or anchor_added:
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
            except Exception as exc:  # attribution is best-effort
                logger.debug("anchor attribution skipped: %s", exc)

        # Did the lineage span more than one git commit? Compute it the same way
        # `roar reproduce` does (commit span), so the two checklists agree.
        single_commit = _session_single_commit(
            roar_dir=request.roar_dir, session_id=collected_lineage.session_id
        )

        # Collapse each job's dataset membership into consumes/produces view edges (a
        # bloom over the touched leaves) and prune the subsumed leaves from the bundle.
        # Pushed after the main registration. Best-effort.
        view_edges_by_job: dict[str, list[Any]] = {}
        composite_leaf_hashes: frozenset[str] = frozenset()
        if not request.dry_run:
            try:
                view_edges_by_job, leaf_hashes = _prepare_view_edges_for_lineage(
                    roar_dir=request.roar_dir,
                    lineage=collected_lineage.lineage,
                    logger=logger,
                )
                composite_leaf_hashes = frozenset(leaf_hashes)
            except Exception as exc:  # view-edge prep is best-effort
                logger.debug("view-edge preparation skipped: %s", exc)

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
            from dataclasses import replace

            preview = preview_register_lineage(
                lineage=collected_lineage.lineage,
                artifact_hash=collected_lineage.artifact_hash,
                prepared=prepared,
                cwd=request.cwd,
                skip_confirmation=request.skip_confirmation,
                confirm_callback=request.confirm_callback,
            )
            return replace(preview, single_commit=single_commit)

        # P1-23: push roar tags BEFORE writing the GLaaS record so the
        # record never references a tag that doesn't yet exist on the
        # canonical remote. Attributed registers fail-closed here.
        #
        # Anonymous registers degrade tag-push failures to warnings: the
        # user explicitly opted into a no-account publish, so requiring
        # git-remote auth (a separate concern from GLaaS) would defeat
        # the whole point. The local tag still gets created; only the
        # push to the canonical remote is skipped on failure.
        from ...integrations.config import config_get
        from .register_tag_push import TagPushError, ensure_roar_tags_pushed

        tag_push_warning: str | None = None
        try:
            tag_summary = ensure_roar_tags_pushed(
                repo_root=prepared.git_tag_repo_root or request.cwd,
                session_commit=prepared.git_context.commit or "",
                session_tag_name=prepared.git_tag_name,
                lineage=collected_lineage.lineage,
                dry_run=False,
                tagging_enabled=prepared.git_tag_name is not None,
                configured_remote=config_get("git.remote"),
                push_mode=str(config_get("git.push_tags_on_register") or "auto"),
                logger=logger,
            )
        except TagPushError as exc:
            if not request.anonymous:
                return RegisterLineageResponse(
                    success=False,
                    artifact_hash=collected_lineage.artifact_hash,
                    error=str(exc),
                )
            tag_push_warning = _format_anonymous_tag_push_warning(str(exc))
            tag_summary = None

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
            composite_leaf_hashes=composite_leaf_hashes,
            view_edges_by_job=view_edges_by_job,
        )

        # Push the consumes view edges now that the jobs + the anchor composite are
        # registered. The view edges are keyed by *local* job UID, but GLaaS stores jobs
        # under publication-scoped *remote* UIDs; translate via the mapping registration
        # persisted to the session metadata. Best-effort: never fails an otherwise-
        # successful registration.
        if result.success and view_edges_by_job and not prepared.registration_session_id:
            remote_uid_by_local = _load_remote_job_uid_mapping(
                roar_dir=request.roar_dir, session_id=collected_lineage.session_id
            )
            for local_job_uid, edges in view_edges_by_job.items():
                remote_job_uid = remote_uid_by_local.get(local_job_uid, local_job_uid)
                try:
                    _push_result, push_error = runtime.glaas_client.register_job_view_edges(
                        remote_job_uid, edges
                    )
                    if push_error:
                        logger.debug(
                            "view-edge push failed for %s (remote %s): %s",
                            local_job_uid,
                            remote_job_uid,
                            push_error,
                        )
                except Exception as exc:  # best-effort
                    logger.debug("view-edge push skipped for %s: %s", local_job_uid, exc)

        warnings: list[str] = []
        if tag_push_warning:
            warnings.append(tag_push_warning)

        return RegisterLineageResponse(
            success=result.success,
            session_hash=result.session_hash,
            session_url=result.session_url,
            artifact_hash=result.artifact_hash,
            jobs_registered=result.jobs_registered,
            jobs_existing=result.jobs_existing,
            artifacts_registered=result.artifacts_registered,
            links_created=result.links_created,
            labels_synced=result.labels_synced,
            already_registered=result.already_registered,
            error=result.error,
            secrets_detected=list(result.secrets_detected),
            secrets_redacted=result.secrets_redacted,
            aborted_by_user=result.aborted_by_user,
            tag_summary=tag_summary,
            warnings=warnings,
            reproducible=bool(prepared.git_context.commit),
            single_commit=single_commit,
        )
    except PublishAuthError as exc:
        return RegisterLineageResponse(success=False, error=str(exc))


def _format_anonymous_tag_push_warning(tag_push_error: str) -> str:
    """One-line tag-push-failure warning for the anonymous register path.

    Anonymous register continues past a push failure (GLaaS publish succeeds).
    The "why a missing push matters" explanation now lives in the reproducibility
    checklist's ``commit reachable on a remote`` item, so this stays terse: name
    that the failure is git-remote auth (not GLaaS), that the record was still
    registered, the fix, and the verbatim git error. (Deliberately not suggesting
    ``git.push_tags_on_register=never`` — that "fix" breaks reproducibility for
    everyone reading the record, it doesn't just silence the warning.)
    """
    return (
        "roar tag push to the git remote failed (git auth, not GLaaS) — registered "
        "without it; the 'commit reachable on a remote' check flags this. "
        f"Fix git remote auth and re-push (`git push <remote> <tag>`). git: {tag_push_error}"
    )


def put_artifacts(request: PutRequest) -> PutResponse:
    """Run the `roar put` application workflow."""
    if not request.dry_run:
        bootstrap(request.roar_dir)
    logger = get_logger()

    if request.anonymous and not request.public:
        return PutResponse(
            success=False,
            destination=request.destination,
            error="Anonymous publication requires public visibility.",
        )

    repo_root = request.repo_root or request.cwd
    # Reproducibility-checklist facts, filled in on the real (non-dry-run) path.
    put_single_commit = True
    put_commit_on_remote = False
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
            runtime_kwargs: dict[str, Any] = {
                "glaas_url": get_glaas_url(),
                "start_dir": str(repo_root),
                "allow_public_without_binding": request.public,
            }
            if request.anonymous:
                runtime_kwargs["force_anonymous"] = True
            runtime = build_publish_runtime(**runtime_kwargs)
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

            # Reproducibility facts for the receipt: same commit-span source as
            # register/reproduce, and whether the commit is reachable on a real
            # remote (git_context.repo is None here when no shareable remote —
            # resolve_roar_git_context drops local file:// URIs).
            from ...utils.git_url import is_shareable_remote

            put_single_commit = _session_single_commit(
                roar_dir=request.roar_dir, session_id=prepared.session_id
            )
            put_commit_on_remote = is_shareable_remote(prepared.git_context.repo)

            from .put_execution import build_put_command

            result = service.put_prepared(
                prepared=prepared,
                sources=request.sources,
                message=request.message,
                dry_run=request.dry_run,
                git_commit=git_commit,
                git_tag=expected_tag,
                declared=request.as_dataset,
                command=build_put_command(
                    request.sources,
                    request.destination,
                    message=request.message,
                    public=request.public,
                    anonymous=request.anonymous,
                    no_tag=request.no_tag,
                    as_dataset=request.as_dataset,
                    step_name=request.step_name,
                ),
            )

            # Apply step name label if provided.
            if request.step_name and result.success and result.job_id:
                with contextlib.suppress(Exception):
                    db_ctx.job_recording._record_step_name_label(result.job_id, request.step_name)

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
        reproducible=bool(git_commit),
        single_commit=put_single_commit,
        commit_on_remote=put_commit_on_remote,
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
