"""Offline registration-package export for local Roar lineage."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ...core.digests import extract_primary_digest
from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...db.context import create_database_context
from ...filters.omit import OmitFilter
from ...integrations.config.raw import get_raw_registration_omit_config
from ..git import resolve_roar_git_context
from .job_preparation import (
    normalize_jobs_for_registration,
    order_jobs_for_registration,
    refresh_job_artifact_references,
)
from .lineage import LineageCollector
from .lineage_composites import build_lineage_composite_payloads
from .registration import normalize_registration_hashes, normalize_registration_source_type
from .secrets import (
    detect_lineage_secrets,
    filter_git_context_secrets,
    filter_lineage_secrets,
)

PACKAGE_FORMAT = "roar.registration_package"
PACKAGE_VERSION = 1
_DIGEST_FIELD_SENTINEL = None


@dataclass(frozen=True)
class ExportRegistrationPackageRequest:
    """Application request for `roar export-registration-package`."""

    output: Path
    roar_dir: Path
    cwd: Path


@dataclass(frozen=True)
class ExportRegistrationPackageResponse:
    """Application response for registration-package export."""

    success: bool
    package_path: str = ""
    package_sha256: str = ""
    job_count: int = 0
    artifact_count: int = 0
    link_count: int = 0
    error: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


def export_registration_package(
    request: ExportRegistrationPackageRequest,
) -> ExportRegistrationPackageResponse:
    """Export the active local session into a Roar registration package."""
    logger = get_logger()

    with create_database_context(request.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()

    if session is None:
        return ExportRegistrationPackageResponse(
            success=False,
            error="No active session. Run 'roar run' to create a session first.",
        )

    session_id = int(session["id"])
    lineage = LineageCollector().collect_session(session_id, request.roar_dir)
    if not lineage.jobs:
        return ExportRegistrationPackageResponse(
            success=False,
            error="No tracked jobs found in the active session.",
        )

    from ...integrations.config import config_get

    git_context = resolve_roar_git_context(
        request.cwd, logger=logger, configured_remote=config_get("git.remote")
    )
    with create_database_context(request.roar_dir) as db_ctx:
        package, encoded = build_registration_package(
            session=session,
            lineage=lineage,
            git_context=git_context,
            cwd=request.cwd,
            db_ctx=db_ctx,
            session_hash=str(session.get("hash") or ""),
            logger=logger,
        )

    request.output.parent.mkdir(parents=True, exist_ok=True)
    request.output.write_bytes(encoded)

    manifest = package["manifest"]
    redaction = package["redaction"]
    return ExportRegistrationPackageResponse(
        success=True,
        package_path=str(request.output),
        package_sha256=str(manifest["package_sha256"]),
        job_count=int(manifest["job_count"]),
        artifact_count=int(manifest["artifact_count"]),
        link_count=int(manifest["link_count"]),
        warnings=list(redaction.get("warnings", [])) if isinstance(redaction, dict) else [],
    )


def build_registration_package(
    *,
    session: dict[str, Any],
    lineage: LineageData,
    git_context: GitContext,
    cwd: Path,
    db_ctx: Any | None = None,
    session_hash: str | None = None,
    composite_builder: Any | None = None,
    logger: ILogger | None = None,
    created_at: str | None = None,
    producer_version: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build a finalized `RoarRegistrationPackageV1` payload and bytes."""
    resolved_logger = logger or get_logger()
    redacted_lineage, redacted_git_context, redaction = _apply_redaction(
        lineage=lineage,
        git_context=git_context,
        cwd=cwd,
    )
    redacted_lineage, redacted_git_context = _drop_local_remotes(
        redacted_lineage, redacted_git_context
    )

    registration_jobs = order_jobs_for_registration(
        normalize_jobs_for_registration(redacted_lineage.jobs)
    )
    refresh_job_artifact_references(registration_jobs, redacted_lineage.artifacts)

    session_record = _build_session_record(session, redacted_git_context)
    job_records = [_build_job_record(job, redacted_git_context) for job in registration_jobs]
    artifact_records = [
        _build_artifact_record(artifact) for artifact in _sort_artifacts(redacted_lineage.artifacts)
    ]
    link_records = _build_link_records(registration_jobs)

    package: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "created_at": created_at or _utc_now_iso(),
        "producer": {
            "name": "roar",
            "version": producer_version or _producer_version(),
        },
        "source": {
            "local_session_id": str(session.get("id")) if session.get("id") is not None else None,
            "git_repo": redacted_git_context.repo,
            "git_commit": redacted_git_context.commit,
            "git_branch": redacted_git_context.branch,
        },
        "manifest": {
            "package_sha256": "",
            "uncompressed_size_bytes": 0,
            "job_count": len(job_records),
            "artifact_count": len(artifact_records),
            "link_count": len(link_records),
        },
        "session": session_record,
        "jobs": job_records,
        "artifacts": artifact_records,
        "links": link_records,
        "redaction": redaction,
    }

    composites = _build_composite_records(
        redacted_lineage.artifacts,
        db_ctx=db_ctx,
        session_hash=session_hash or str(session.get("hash") or ""),
        composite_builder=composite_builder,
        logger=resolved_logger,
    )
    if composites:
        package["composites"] = composites

    return finalize_registration_package(package)


def finalize_registration_package(package: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """Fill manifest digest and exact serialized size for a package."""
    finalized = copy.deepcopy(package)
    manifest = finalized.setdefault("manifest", {})
    manifest["package_sha256"] = "0" * 64
    manifest["uncompressed_size_bytes"] = 0

    size = 0
    for _ in range(10):
        manifest["uncompressed_size_bytes"] = size
        manifest["package_sha256"] = compute_registration_package_sha256(finalized)
        encoded = serialize_registration_package(finalized)
        next_size = len(encoded)
        if next_size == size:
            return finalized, encoded
        size = next_size

    manifest["uncompressed_size_bytes"] = size
    manifest["package_sha256"] = compute_registration_package_sha256(finalized)
    encoded = serialize_registration_package(finalized)
    return finalized, encoded


def serialize_registration_package(package: dict[str, Any]) -> bytes:
    """Serialize a package in deterministic JSON form."""
    return json.dumps(
        _json_safe(package),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_registration_package_sha256(package: dict[str, Any]) -> str:
    """Compute the package digest, excluding only the self-referential digest field."""
    digest_payload = copy.deepcopy(package)
    manifest = digest_payload.setdefault("manifest", {})
    manifest["package_sha256"] = _DIGEST_FIELD_SENTINEL
    return hashlib.sha256(serialize_registration_package(digest_payload)).hexdigest()


def _apply_redaction(
    *,
    lineage: LineageData,
    git_context: GitContext,
    cwd: Path,
) -> tuple[LineageData, GitContext, dict[str, Any]]:
    omit_config = get_raw_registration_omit_config(start_dir=str(cwd))
    omit_filter = OmitFilter(omit_config) if omit_config.get("enabled", True) else None

    detected = detect_lineage_secrets(
        lineage=lineage,
        git_context=git_context,
        omit_filter=omit_filter,
    )
    filtered_lineage = filter_lineage_secrets(lineage=lineage, omit_filter=omit_filter)
    filtered_git_context, git_detections = filter_git_context_secrets(
        git_context=git_context, omit_filter=omit_filter
    )
    detected.extend(git_detections)
    filtered_lineage, job_git_detections = _filter_job_git_repos(filtered_lineage, omit_filter)
    detected.extend(job_git_detections)

    applied = bool(detected) or serialize_registration_package(
        _lineage_payload(filtered_lineage)
    ) != (serialize_registration_package(_lineage_payload(lineage)))
    warning_ids = sorted(set(detected))
    return (
        filtered_lineage,
        filtered_git_context,
        {
            "applied": applied,
            "warnings": [_redaction_warning(pattern_id) for pattern_id in warning_ids],
        },
    )


def _drop_local_remotes(
    lineage: LineageData, git_context: GitContext
) -> tuple[LineageData, GitContext]:
    """Don't publish non-shareable (``file://`` / local-path) git repos to GLaaS.

    A repo with no real remote records its own ``file://`` path as ``git_repo``;
    publishing that leaks the local filesystem path and is useless to anyone else.
    Replace such repos with "" so the GLaaS record honestly shows no remote — the
    reproducibility checklist already flags this as "commit reachable on a remote".
    """
    from ...utils.git_url import is_shareable_remote

    if git_context.repo and not is_shareable_remote(git_context.repo):
        git_context = GitContext(repo="", commit=git_context.commit, branch=git_context.branch)
    for job in lineage.jobs:
        repo = job.get("git_repo")
        if isinstance(repo, str) and repo and not is_shareable_remote(repo):
            job["git_repo"] = ""
    return lineage, git_context


def _filter_job_git_repos(
    lineage: LineageData,
    omit_filter: OmitFilter | None,
) -> tuple[LineageData, list[str]]:
    if omit_filter is None:
        return lineage, []

    detections: list[str] = []
    filtered_jobs: list[dict[str, Any]] = []
    for job in lineage.jobs:
        filtered_job = dict(job)
        git_repo = filtered_job.get("git_repo")
        if isinstance(git_repo, str) and git_repo:
            filtered_repo, repo_detections = omit_filter.filter_git_url(git_repo)
            filtered_job["git_repo"] = filtered_repo
            detections.extend(repo_detections)
        filtered_jobs.append(filtered_job)

    return (
        LineageData(
            jobs=filtered_jobs,
            artifacts=lineage.artifacts,
            artifact_hashes=lineage.artifact_hashes,
            pipeline=lineage.pipeline,
        ),
        detections,
    )


def _redaction_warning(pattern_id: str) -> dict[str, Any]:
    code = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in pattern_id)
    return {
        "code": f"roar.redaction.{code}",
        "message": f"Redacted lineage data matching {pattern_id}.",
        "severity": "warning",
    }


def _lineage_payload(lineage: LineageData) -> dict[str, Any]:
    return {
        "jobs": lineage.jobs,
        "artifacts": lineage.artifacts,
        "pipeline": lineage.pipeline,
    }


def _build_session_record(session: dict[str, Any], git_context: GitContext) -> dict[str, Any]:
    record = _json_safe(dict(session))
    record["local_session_id"] = str(session.get("id")) if session.get("id") is not None else None
    record["git"] = {
        "repo": git_context.repo,
        "commit": git_context.commit,
        "branch": git_context.branch,
    }
    return record


def _build_job_record(job: dict[str, Any], git_context: GitContext) -> dict[str, Any]:
    record: dict[str, Any] = {
        "local_id": job.get("id"),
        "job_uid": job.get("job_uid"),
        "parent_job_uid": job.get("parent_job_uid"),
        "timestamp": job.get("timestamp"),
        "command": job.get("command") or "",
        "script": job.get("script"),
        "step_number": job.get("step_number") or 0,
        "step_name": job.get("step_name"),
        "git_repo": job.get("git_repo") or git_context.repo,
        "git_commit": job.get("git_commit") or git_context.commit or "",
        "git_branch": job.get("git_branch") or git_context.branch or "",
        "duration_seconds": job.get("duration_seconds") or 0.0,
        "exit_code": job.get("exit_code") or 0,
        "status": job.get("status"),
        "execution_backend": job.get("execution_backend"),
        "execution_role": job.get("execution_role"),
        "job_type": job.get("job_type") or "run",
    }
    for optional_key in ("metadata", "telemetry"):
        if job.get(optional_key) is not None:
            record[optional_key] = job[optional_key]
    return _json_safe(record)


def _build_artifact_record(artifact: dict[str, Any]) -> dict[str, Any]:
    hashes = normalize_registration_hashes(artifact, fallback_to_hash=True)
    record: dict[str, Any] = {
        "local_id": artifact.get("id"),
        "hash": extract_primary_digest({"hashes": hashes}),
        "hashes": hashes,
        "size": _coerce_nonnegative_int(artifact.get("size")),
        "source_type": normalize_registration_source_type(artifact.get("source_type")),
        "source_url": artifact.get("source_url"),
        "first_seen_at": artifact.get("first_seen_at"),
        "first_seen_path": artifact.get("first_seen_path") or artifact.get("path"),
        "kind": artifact.get("kind") or "primitive",
        "component_count": artifact.get("component_count"),
    }
    for optional_key in ("metadata", "uploaded_to"):
        if artifact.get(optional_key) is not None:
            record[optional_key] = artifact[optional_key]
    return _json_safe(record)


def _build_link_records(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for job in jobs:
        job_uid = job.get("job_uid")
        if not isinstance(job_uid, str) or not job_uid:
            continue
        for direction, key in (("input", "_inputs"), ("output", "_outputs")):
            items = job.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                artifact_hash = _link_artifact_hash(item)
                if not artifact_hash:
                    continue
                link: dict[str, Any] = {
                    "job_uid": job_uid,
                    "direction": direction,
                    "artifact_hash": artifact_hash,
                    "path": item.get("path"),
                }
                for optional_key in ("byte_ranges", "size", "source_type", "metadata"):
                    if item.get(optional_key) is not None:
                        link[optional_key] = item[optional_key]
                links.append(_json_safe(link))
    return links


def _link_artifact_hash(item: dict[str, Any]) -> str | None:
    for key in ("artifact_hash", "hash", "artifact_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value.lower()

    hashes = item.get("hashes")
    if isinstance(hashes, list):
        return extract_primary_digest({"hashes": hashes})
    return None


def _build_composite_records(
    artifacts: list[dict[str, Any]],
    *,
    db_ctx: Any | None,
    session_hash: str,
    composite_builder: Any | None,
    logger: ILogger,
) -> list[dict[str, Any]]:
    if db_ctx is not None:
        from .composite_builder import CompositeArtifactBuilder

        payloads = build_lineage_composite_payloads(
            db_ctx=db_ctx,
            lineage_artifacts=artifacts,
            session_hash=session_hash,
            composite_builder=composite_builder or CompositeArtifactBuilder(),
            logger=logger,
        )
        return [_json_safe(item.payload) for item in payloads]

    records: list[dict[str, Any]] = []
    for artifact in _sort_artifacts(artifacts):
        if str(artifact.get("kind") or "primitive") != "composite":
            continue
        hashes = normalize_registration_hashes(artifact, fallback_to_hash=True)
        digest = extract_primary_digest({"hashes": hashes})
        if not digest:
            logger.debug("Skipping composite package detail without digest")
            continue
        records.append(
            _json_safe(
                {
                    "artifact_hash": digest,
                    "local_id": artifact.get("id"),
                    "component_count": artifact.get("component_count"),
                    "root_path": artifact.get("first_seen_path") or artifact.get("path"),
                }
            )
        )
    return records


def _sort_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        artifacts,
        key=lambda artifact: (
            str(_link_artifact_hash(artifact) or ""),
            str(artifact.get("id") or ""),
        ),
    )


def _coerce_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _utc_now_iso(now: Callable[[], datetime] | None = None) -> str:
    current = now() if now else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _producer_version() -> str:
    try:
        return version("roar-cli")
    except Exception:
        return "unknown"
