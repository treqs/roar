"""Application orchestration for `roar get` workflows."""

from __future__ import annotations

import time
from typing import Any, cast

from ...application.git import build_roar_git_tag_name, create_roar_git_tag, resolve_git_state
from ...core.bootstrap import bootstrap
from ...core.logging import get_logger
from ...core.operation_metadata import build_operation_metadata_json
from ...db.context import create_database_context
from ...execution.recording import LocalJobRecorder, LocalRecordedArtifact
from ...integrations.download import parse_source, resolve_download_backend
from .requests import GetRequest
from .results import GetDownloadedFile, GetResponse
from .transfer import GetService, GetTransferResult


def get_artifacts(request: GetRequest) -> GetResponse:
    """Execute the `roar get` application workflow."""
    bootstrap(request.roar_dir)
    logger = get_logger()

    parsed_source = parse_source(request.source)
    backend = resolve_download_backend(request.source)
    repo_root = request.repo_root or request.cwd
    is_prefix = request.source.rstrip("/") != request.source or parsed_source.is_prefix

    git_commit = None
    git_branch = None
    git_repo_url = None
    if not request.dry_run:
        try:
            git_commit = resolve_git_state(repo_root).commit
            logger.debug("Git commit: %s", git_commit)
        except Exception as exc:
            logger.debug("Git operation failed (non-fatal for get): %s", exc)
        try:
            import subprocess

            git_branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(repo_root),
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                or None
            )
            git_repo_url = (
                subprocess.check_output(
                    ["git", "remote", "get-url", "origin"],
                    cwd=str(repo_root),
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                or None
            )
        except Exception:
            pass

    git_branch = None
    git_repo_url = None
    if not request.dry_run:
        try:
            import subprocess

            git_branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(repo_root),
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                or None
            )
            git_repo_url = (
                subprocess.check_output(
                    ["git", "remote", "get-url", "origin"],
                    cwd=str(repo_root),
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                or None
            )
        except Exception:
            pass

    with create_database_context(request.roar_dir) as db_ctx:
        service = GetService(
            backend=backend,
            source=parsed_source,
            repo_root=repo_root,
        )
        t0 = time.time()
        transfer_result = service.get(
            destination=request.destination,
            expected_hash=request.expected_hash,
            dry_run=request.dry_run,
            force=request.force,
            is_prefix=is_prefix,
        )
        download_duration = time.time() - t0

        result = _materialize_get_result(
            db_ctx=db_ctx,
            request=request,
            parsed_source=parsed_source,
            transfer_result=transfer_result,
            git_commit=git_commit,
            git_branch=git_branch,
            git_repo=git_repo_url,
            duration_seconds=download_duration,
            backend=backend,
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

    return GetResponse(
        success=result.success,
        source=request.source,
        job_id=result.job_id,
        job_uid=result.job_uid,
        downloaded_files=result.downloaded_files,
        dry_run=result.dry_run,
        would_download=result.would_download,
        git_tag=git_tag_name,
        warnings=warnings,
        error=result.error,
    )


def _materialize_get_result(
    *,
    db_ctx,
    request: GetRequest,
    parsed_source,
    transfer_result: GetTransferResult,
    git_commit: str | None,
    git_branch: str | None = None,
    git_repo: str | None = None,
    duration_seconds: float = 0.0,
    backend=None,
) -> GetResponse:
    if transfer_result.dry_run or not transfer_result.success:
        return GetResponse(
            success=transfer_result.success,
            source=request.source,
            downloaded_files=transfer_result.downloaded_files,
            dry_run=transfer_result.dry_run,
            would_download=transfer_result.would_download,
            error=transfer_result.error,
        )

    metadata_json = _build_get_operation_metadata_json(
        request=request,
        parsed_source=parsed_source,
        downloaded_files=transfer_result.downloaded_files,
        git_commit=git_commit,
    )
    recorder = LocalJobRecorder()
    output_artifacts = [
        LocalRecordedArtifact(
            path=file_info.local_path,
            hashes={"blake3": str(file_info.hash)},
            size=int(file_info.size or 0),
        )
        for file_info in transfer_result.downloaded_files
    ]
    job_id, job_uid = recorder.record(
        db_ctx,
        command=_build_get_command(request),
        timestamp=time.time(),
        metadata=metadata_json,
        execution_backend="local",
        execution_role="host",
        job_type="get",
        output_artifacts=output_artifacts,
        exit_code=0,
        git_commit=git_commit,
        git_branch=git_branch,
        git_repo=git_repo,
        duration_seconds=duration_seconds,
        step_name=request.step_name,
    )

    try:
        _register_get_composite(db_ctx=db_ctx, backend=backend, job_id=job_id)
    except Exception as exc:  # composite formation is best-effort; never fail a get
        get_logger().debug("get composite formation skipped: %s", exc)

    return GetResponse(
        success=True,
        source=request.source,
        job_id=job_id,
        job_uid=job_uid,
        downloaded_files=transfer_result.downloaded_files,
    )


def _register_get_composite(*, db_ctx, backend, job_id: int):
    """Form + register a composite for an HF dataset get (boundary-time, local).

    Builds a ``composite-sha256`` over the dataset's LFS files (sha256 from the HF
    manifest — content identity without re-hashing), excluding repo/format
    boilerplate, and records it as an output of the get job with the HF coordinates
    in metadata. Skips non-HF backends and anything that isn't a >=2-file dataset.
    """
    import json
    import mimetypes

    from ...application.composite import detect
    from ...application.publish.composite_builder import (
        CompositeArtifactBuilder,
        CompositeLeaf,
    )
    from ...db.context import optional_repo

    manifest_fn = getattr(backend, "manifest", None)
    coords_attr = getattr(backend, "coordinates", None)
    if manifest_fn is None or coords_attr is None:
        return None  # not an HF backend

    subpath = (getattr(backend, "_subpath", "") or "").rstrip("/")

    def _rel(path: str) -> str:
        if subpath and (path == subpath or path.startswith(subpath + "/")):
            return path[len(subpath) + 1 :] or path
        return path

    files = [
        f
        for f in manifest_fn()
        if not subpath or f.path == subpath or f.path.startswith(subpath + "/")
    ]
    boilerplate = set(detect([_rel(f.path) for f in files]).boilerplate)
    leaves = [
        CompositeLeaf(
            relative_path=_rel(f.path),
            digest=f.sha256,
            size=f.size,
            component_type=mimetypes.guess_type(_rel(f.path))[0],
            leaf_kind="file",
            component_algorithm="sha256",
        )
        for f in files
        if f.is_lfs and f.sha256 and _rel(f.path) not in boilerplate
    ]
    if len(leaves) < 2:
        return None  # a lone file (or all-boilerplate) is not a composite

    coords = coords_attr
    root_label = f"hf://{coords['repo_type']}/{coords['repo']}@{coords['commit']}"
    result = CompositeArtifactBuilder().build_for_leaves(
        root_path=root_label,
        leaves=leaves,
        session_hash="",
        source_type="hf",
    )
    if result is None:
        return None

    metadata = json.dumps(
        {
            "composite": {
                "root_path": root_label,
                "component_count_total": result.component_count_total,
                "component_count_stored": result.component_count_stored,
            },
            "hf": coords,
        }
    )
    artifact_id, _created = db_ctx.artifacts.register(
        hashes={result.payload["hashes"][0]["algorithm"]: result.digest},
        size=int(result.payload.get("size") or 0),
        path=root_label,
        source_type="hf",
        metadata=metadata,
    )
    composite_repo = cast(Any, optional_repo(db_ctx, "composites"))
    if composite_repo is not None:
        composite_repo.upsert_details(
            artifact_id=artifact_id,
            components=list(result.payload.get("components") or []),
            component_count_total=result.component_count_total,
            membership_index=result.payload.get("membership_index"),
        )
    db_ctx.jobs.add_output(job_id, artifact_id, root_label)
    return result


def _build_get_command(request: GetRequest) -> str:
    command = f"roar get {request.source}"
    if request.message:
        command += f' -m "{request.message}"'
    return command


def _build_get_operation_metadata_json(
    *,
    request: GetRequest,
    parsed_source,
    downloaded_files: list[GetDownloadedFile],
    git_commit: str | None,
) -> str:
    artifact_urls: dict[str, str] = {}
    for file_info in downloaded_files:
        artifact_urls[file_info.local_path] = file_info.remote_url

    return build_operation_metadata_json(
        "get",
        {
            "source": request.source,
            "source_type": parsed_source.scheme,
            "message": request.message,
            "artifacts": artifact_urls,
            "git_commit": git_commit,
            "git_tag": None,
            "timestamp": time.time(),
        },
    )
