"""Application orchestration for `roar get` workflows."""

from __future__ import annotations

import time

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
    if not request.dry_run:
        try:
            git_commit = resolve_git_state(repo_root).commit
            logger.debug("Git commit: %s", git_commit)
        except Exception as exc:
            logger.debug("Git operation failed (non-fatal for get): %s", exc)

    with create_database_context(request.roar_dir) as db_ctx:
        service = GetService(
            backend=backend,
            source=parsed_source,
            repo_root=repo_root,
        )
        transfer_result = service.get(
            destination=request.destination,
            expected_hash=request.expected_hash,
            dry_run=request.dry_run,
            force=request.force,
            is_prefix=is_prefix,
        )

        result = _materialize_get_result(
            db_ctx=db_ctx,
            request=request,
            parsed_source=parsed_source,
            transfer_result=transfer_result,
            git_commit=git_commit,
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
    )
    return GetResponse(
        success=True,
        source=request.source,
        job_id=job_id,
        job_uid=job_uid,
        downloaded_files=transfer_result.downloaded_files,
    )


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
