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
from ...integrations.download.base import DownloadError
from .requests import GetRequest
from .results import GetDownloadedFile, GetResponse
from .transfer import GetService, GetTransferResult


def get_artifacts(request: GetRequest) -> GetResponse:
    """Execute the `roar get` application workflow."""
    bootstrap(request.roar_dir)
    logger = get_logger()

    parsed_source = parse_source(request.source)  # canonical — always what we RECORD
    # Fetch location: the mirror (`--cache`) when given, else the canonical source.
    # Recording below always uses the canonical `parsed_source`, so the artifact's
    # source_url / the AI-BOM downloadLocation stay the public URL, not the mirror.
    fetch_url = request.cache or request.source
    backend = resolve_download_backend(fetch_url)
    fetch_parsed = parse_source(fetch_url)
    repo_root = request.repo_root or request.cwd
    is_prefix = fetch_url.rstrip("/") != fetch_url or fetch_parsed.is_prefix

    # A `--cache` mirror is only integrity-bound to the canonical identity when a
    # `--hash` is given. Without it, the mirror's bytes are recorded under the
    # canonical source_url unverified — warn so provenance isn't silently trusted.
    if request.cache and not request.expected_hash:
        logger.warning(
            "--cache %s used without --hash: the mirror's bytes are not integrity-checked "
            "against the canonical source %s, yet are recorded under it. Pass --hash "
            "<digest> to bind the download to a known hash.",
            request.cache,
            request.source,
        )

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
        t0 = time.time()
        fetched_from_cache = False
        try:
            transfer_result = GetService(
                backend=backend,
                source=fetch_parsed,
                repo_root=repo_root,
            ).get(
                destination=request.destination,
                expected_hash=request.expected_hash,
                dry_run=request.dry_run,
                force=request.force,
                is_prefix=is_prefix,
                limit=request.limit,
            )
            fetched_from_cache = bool(request.cache)
            # The transfer layer REPORTS a hash mismatch (and other soft failures)
            # as success=False rather than raising, so trigger the same cache
            # fallback here — otherwise a corrupt mirror hard-fails the whole get
            # even when the canonical source would succeed (the documented behavior).
            if request.cache and not transfer_result.dry_run and not transfer_result.success:
                raise DownloadError(transfer_result.error or "cache fetch was unsuccessful")
        except (DownloadError, FileNotFoundError, ValueError, OSError) as exc:
            # A `--cache` mirror is best-effort: on miss / unreachable / hash
            # mismatch, fall back to the canonical source (still hash-verified via
            # --hash if given, and by content-addressing at reproduce time). A get
            # without a cache re-raises unchanged.
            if not request.cache:
                raise
            logger.warning(
                "cache fetch from %s failed (%s); falling back to canonical source %s",
                request.cache,
                exc,
                request.source,
            )
            fetched_from_cache = False
            canonical_is_prefix = (
                request.source.rstrip("/") != request.source or parsed_source.is_prefix
            )
            transfer_result = GetService(
                backend=resolve_download_backend(request.source),
                source=parsed_source,
                repo_root=repo_root,
            ).get(
                destination=request.destination,
                expected_hash=request.expected_hash,
                dry_run=request.dry_run,
                force=request.force,
                is_prefix=canonical_is_prefix,
                limit=request.limit,
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
            fetched_from_cache=fetched_from_cache,
            fetch_url=fetch_url,
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
        anchor_lfs_only_mb=result.anchor_lfs_only_mb,
        error=result.error,
    )


def _canonical_source_url(file_info, request: GetRequest, fetch_url, fetched_from_cache: bool):
    """The canonical (recorded) URL for one downloaded file.

    When the bytes came from the canonical source (no ``--cache``, or a cache
    miss/mismatch that fell back), each file's own fetch URL is already canonical.
    When they came from a ``--cache`` mirror, remap each file's mirror URL back
    onto the canonical prefix, so a PREFIX get records each file's own canonical
    object (``s3://canon/data/a.pt``) rather than the bare prefix for every file.
    """
    remote_url = file_info.remote_url or ""
    if not fetched_from_cache:
        return remote_url or request.source
    # Fetched from the mirror: swap the mirror prefix for the canonical one.
    if fetch_url and remote_url.startswith(fetch_url) and remote_url != fetch_url:
        suffix = remote_url[len(fetch_url) :]
        return request.source.rstrip("/") + "/" + suffix.lstrip("/")
    # Single-file cache (or an unmappable URL): the file itself IS the canonical source.
    return request.source


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
    fetched_from_cache: bool = False,
    fetch_url: str | None = None,
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

    # Form the full-dataset *anchor* composite *before* recording the job so the job
    # links to the anchor rather than to individual leaf files. A partial get is
    # recorded as a *view* of the anchor (the selector below), never as its own subset
    # composite. Best-effort: a composite never makes-or-breaks the get.
    composite: tuple[str, str, str] | None = None
    anchor_notices: dict[str, Any] = {}
    try:
        composite = _form_get_composite(
            db_ctx=db_ctx,
            backend=backend,
            full_anchor=request.full_anchor,
            notices=anchor_notices,
        )
    except Exception as exc:  # composite formation is best-effort; never fail a get
        get_logger().debug("get composite formation skipped: %s", exc)

    # A partial get (`--limit N`) materializes only the first N data files; record that
    # as a replayable selector over the anchor rather than minting a subset composite.
    selector = (
        f"first:{request.limit}" if request.limit is not None and request.limit >= 0 else None
    )
    anchor_digest = composite[2] if composite is not None else None
    metadata_json = _build_get_operation_metadata_json(
        request=request,
        parsed_source=parsed_source,
        downloaded_files=transfer_result.downloaded_files,
        git_commit=git_commit,
        anchor=anchor_digest,
        selector=selector,
    )

    recorder = LocalJobRecorder()
    if composite is not None:
        # A get job links to the composite artifact, not its subfiles — the
        # downloaded files are the composite's components.
        output_artifacts: list[LocalRecordedArtifact] = []
    else:
        # Record the canonical source on each downloaded artifact so it is a
        # SOURCE node with a known origin (source_type/source_url), not a bare
        # local file. This is what a downstream AI-BOM derives `downloadLocation`
        # from — so it must be each file's OWN canonical URL, not a shared prefix.
        source_type = parsed_source.scheme or None
        output_artifacts = [
            LocalRecordedArtifact(
                path=file_info.local_path,
                hashes={"blake3": str(file_info.hash)},
                size=int(file_info.size or 0),
                source_type=source_type,
                source_url=_canonical_source_url(file_info, request, fetch_url, fetched_from_cache),
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

    if composite is not None:
        composite_artifact_id, composite_root_label, _anchor_digest = composite
        db_ctx.jobs.add_output(job_id, composite_artifact_id, composite_root_label)
        try:
            _register_get_crosswalk_rows(
                db_ctx=db_ctx,
                backend=backend,
                downloaded_files=transfer_result.downloaded_files,
            )
        except Exception as exc:  # crosswalk is best-effort; never fail a get
            get_logger().debug("get crosswalk registration skipped: %s", exc)

    return GetResponse(
        success=True,
        source=request.source,
        job_id=job_id,
        job_uid=job_uid,
        downloaded_files=transfer_result.downloaded_files,
        anchor_lfs_only_mb=anchor_notices.get("lfs_only_mb"),
    )


def _form_get_composite(*, db_ctx, backend, full_anchor=False, notices=None):
    """Form + register the full-dataset *anchor* composite for an HF get.

    Builds the canonical ``sha256-tree`` over the dataset's data + format-declared
    meta (LFS sha256 from the HF manifest; identity-bearing non-LFS files re-hashed
    within a byte budget), excluding repo/format boilerplate, and registers it locally
    with the HF coordinates in metadata. The anchor is the full dataset at this commit
    regardless of how many files this get actually downloaded: a partial get is
    recorded by the caller as a *view* of the anchor (job link + selector), never as
    its own subset composite. Skips non-HF backends and anything that isn't a >=2-file
    structural dataset.

    When the total size of identity-bearing non-LFS files exceeds the re-hash budget,
    they are excluded from the anchor (LFS-only identity) unless ``full_anchor`` is set,
    which lifts the cap and downloads them. The excluded megabytes are recorded in
    ``notices['lfs_only_mb']`` so the caller can warn + hint.

    Returns ``(artifact_id, root_label, digest)`` for the caller to link as the get
    job's output, or ``None`` when no composite forms. The leaf files are the anchor's
    components, not standalone job outputs — a get job links to the anchor.
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
    detection = detect([_rel(f.path) for f in files])
    boilerplate = set(detection.boilerplate)

    # Identity-bearing files: data + format-declared meta, excluding boilerplate.
    # LFS files use the published sha256 (free); identity-bearing non-LFS files
    # (e.g. LeRobot meta/) are re-hashed to sha256 from a small download, gated by a
    # byte budget so a pile of small non-LFS files can't trigger a huge fetch.
    _NONLFS_REHASH_BUDGET = 64 * 1024 * 1024
    identity = sorted((f for f in files if _rel(f.path) not in boilerplate), key=lambda f: f.path)
    nonlfs_identity = [f for f in identity if not (f.is_lfs and f.sha256)]
    nonlfs_bytes = sum(f.size for f in nonlfs_identity)
    # --full-anchor lifts the cap and downloads the non-LFS identity files; otherwise
    # they are dropped from identity when over budget (and the caller is told).
    rehash_nonlfs = bool(full_anchor) or nonlfs_bytes <= _NONLFS_REHASH_BUDGET
    if nonlfs_identity and not rehash_nonlfs and notices is not None:
        notices["lfs_only_mb"] = nonlfs_bytes / 1e6
    sha256_of = getattr(backend, "sha256_of", None)

    leaves: list[CompositeLeaf] = []
    for f in identity:
        rel = _rel(f.path)
        if f.is_lfs and f.sha256:
            digest = f.sha256  # asserted from HF metadata
        elif rehash_nonlfs and sha256_of is not None:
            digest = sha256_of(f.path)  # re-hashed from bytes, verified
        else:
            continue  # non-LFS over budget — left out (LFS-only identity)
        leaves.append(
            CompositeLeaf(
                relative_path=rel,
                digest=digest,
                size=f.size,
                component_type=mimetypes.guess_type(rel)[0],
                leaf_kind="file",
                component_algorithm="sha256",
            )
        )
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

    metadata_payload: dict[str, Any] = {
        "composite": {
            "root_path": root_label,
            "component_count_total": result.component_count_total,
            "component_count_stored": result.component_count_stored,
        },
        "hf": coords,
    }
    metadata = json.dumps(metadata_payload)
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
    return artifact_id, root_label, result.digest


def _register_get_crosswalk_rows(*, db_ctx, backend, downloaded_files) -> None:
    """Register each downloaded shard as a local artifact with its blake3 + a crosswalk.

    The shard's published identity is its ``blake3`` (computed during transfer, the
    local rename-stable identity). Its ``sha256`` (the HF LFS oid, the anchor's leaf
    identity) is the **local crosswalk**: a later run that reads the shard is recognized
    by ``blake3`` and resolved to its ``sha256`` — and thus to anchor membership —
    without re-hashing.

    The crosswalk ``sha256`` is stored in the artifact's **metadata**, not as a second
    hash, because registration never serializes metadata but does serialize all hashes:
    publishing ``blake3`` and ``sha256`` on one artifact would assert the cross-algo
    pairing on GLaaS, which the hash-boundary design forbids. Keeping it in metadata
    means no registration path (batch, bearer, or offline package) can leak it.

    The shards are not job outputs (the get job links to the anchor); these rows exist
    purely so downstream attribution can bridge the two hash planes locally.
    """
    import json

    sha256_by_path = getattr(backend, "_sha256_by_path", None)
    if sha256_by_path is None:
        return  # not an HF backend
    oid_map = sha256_by_path()  # {manifest_path: sha256}
    for file_info in downloaded_files:
        remote_key = getattr(file_info, "remote_key", None)
        blake3_digest = file_info.hash
        if not remote_key or not blake3_digest:
            continue
        sha256_digest = oid_map.get(remote_key)
        if sha256_digest is None:
            continue  # non-LFS / unknown — no published sha256 to cross-walk
        db_ctx.artifacts.register(
            hashes={"blake3": str(blake3_digest)},
            size=int(file_info.size or 0),
            path=file_info.local_path,
            metadata=json.dumps({"origin": {"algorithm": "sha256", "digest": sha256_digest}}),
        )


def _build_get_command(request: GetRequest) -> str:
    """Reconstruct the user's `roar get` invocation for the job record.

    Includes the flags that change *what* gets fetched/recorded (--limit, --name,
    --tag, --force) so the recorded command faithfully reflects a subset get rather
    than collapsing to a bare `roar get <source>`. The destination is omitted: it's
    resolved to an absolute, environment-specific path that wouldn't match what the
    user typed.
    """
    parts = ["roar get", request.source]
    if request.cache:
        # Record the mirror so a reproduce replays cache-first-then-canonical.
        parts.append(f"--cache {request.cache}")
    if request.limit is not None:
        parts.append(f"--limit {request.limit}")
    if request.step_name:
        parts.append(f'-n "{request.step_name}"')
    if getattr(request, "tag", False):
        parts.append("--tag")
    if getattr(request, "force", False):
        parts.append("--force")
    if request.message:
        parts.append(f'-m "{request.message}"')
    return " ".join(parts)


def _build_get_operation_metadata_json(
    *,
    request: GetRequest,
    parsed_source,
    downloaded_files: list[GetDownloadedFile],
    git_commit: str | None,
    anchor: str | None = None,
    selector: str | None = None,
) -> str:
    artifact_urls: dict[str, str] = {}
    for file_info in downloaded_files:
        artifact_urls[file_info.local_path] = file_info.remote_url

    payload: dict[str, Any] = {
        "source": request.source,
        "source_type": parsed_source.scheme,
        "message": request.message,
        "artifacts": artifact_urls,
        "git_commit": git_commit,
        "git_tag": None,
        "timestamp": time.time(),
    }
    # A partial get is a view of the full-dataset anchor: record the anchor's
    # sha256-tree digest and the replayable selector so the subset is queryable
    # without minting a separate subset composite.
    if anchor is not None:
        payload["anchor"] = anchor
    if selector is not None:
        payload["selector"] = selector
    return build_operation_metadata_json("get", payload)
