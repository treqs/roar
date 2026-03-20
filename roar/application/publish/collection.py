"""Application-owned register target collection workflows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...db.context import create_database_context
from ...db.hashing.backend import compute_hashes_batch
from ...db.query_context import create_query_database_context
from .lineage import LineageCollector
from .session import PublishSessionService
from .targets import (
    ResolvedRegisterTarget,
    parse_register_step_reference,
    resolve_publish_artifact_path,
    select_publish_artifact_hash,
)


@dataclass(frozen=True)
class CollectedRegisterLineage:
    """Collected local lineage ready for registration."""

    lineage: LineageData
    session_id: int | None
    artifact_hash: str
    session_hash_override: str | None = None


def collect_register_lineage(
    *,
    target: ResolvedRegisterTarget,
    roar_dir: Path,
    cwd: Path,
    lineage_collector: LineageCollector,
    session_service: PublishSessionService,
    logger: ILogger,
    dry_run: bool = False,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    """Collect local lineage for a resolved register target."""
    if target.kind == "step_reference":
        return _collect_step_lineage(
            step_reference=target.value,
            roar_dir=roar_dir,
            lineage_collector=lineage_collector,
            dry_run=dry_run,
        )
    if target.kind == "job_uid":
        return _collect_job_lineage(
            job_uid=target.value,
            roar_dir=roar_dir,
            lineage_collector=lineage_collector,
        )
    if target.kind == "artifact_hash":
        return _collect_artifact_hash_lineage(
            artifact_hash=target.value,
            roar_dir=roar_dir,
            lineage_collector=lineage_collector,
        )
    if target.kind == "session_hash":
        return _collect_session_lineage(
            session_hash=target.value,
            roar_dir=roar_dir,
            lineage_collector=lineage_collector,
            session_service=session_service,
        )
    if target.kind == "artifact_path":
        return _collect_artifact_path_lineage(
            artifact_path=target.value,
            roar_dir=roar_dir,
            cwd=cwd,
            lineage_collector=lineage_collector,
            logger=logger,
        )
    return None, f"Unsupported register target type: {target.kind}"


def _collect_step_lineage(
    *,
    step_reference: str,
    roar_dir: Path,
    lineage_collector: LineageCollector,
    dry_run: bool,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    parsed = parse_register_step_reference(step_reference)
    if parsed is None:
        return None, f"Invalid DAG reference: {step_reference}"
    step_number, is_build = parsed

    if dry_run:
        with create_query_database_context(roar_dir) as db_ctx:
            session = db_ctx.sessions.get_active()
            if not session:
                return None, "No active session. Run 'roar run' to create a session first."
            session_id = int(session["id"])

        lineage = lineage_collector.collect_step_read_only(
            session_id=session_id,
            step_number=step_number,
            roar_dir=roar_dir,
            job_type="build" if is_build else None,
        )
    else:
        with create_database_context(roar_dir) as db_ctx:
            session = db_ctx.sessions.get_active()
            if not session:
                return None, "No active session. Run 'roar run' to create a session first."
            session_id = int(session["id"])

        lineage = lineage_collector.collect_step(
            session_id=session_id,
            step_number=step_number,
            roar_dir=roar_dir,
            job_type="build" if is_build else None,
        )

    if not lineage.jobs:
        return None, f"No tracked jobs found for DAG reference {step_reference}."

    return (
        CollectedRegisterLineage(
            lineage=lineage,
            session_id=int(lineage.pipeline["id"]) if lineage.pipeline else None,
            artifact_hash=select_representative_hash(lineage),
        ),
        None,
    )


def _collect_session_lineage(
    *,
    session_hash: str,
    roar_dir: Path,
    lineage_collector: LineageCollector,
    session_service: PublishSessionService,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    with create_database_context(roar_dir) as db_ctx:
        session, resolved_hash, error = resolve_local_session_target(
            db_ctx=db_ctx,
            roar_dir=roar_dir,
            session_hash=session_hash,
            session_service=session_service,
        )
        if session is None:
            return None, error or "Session not found."
        lineage = lineage_collector.collect_session(int(session["id"]), roar_dir)

    return (
        CollectedRegisterLineage(
            lineage=lineage,
            session_id=int(session["id"]),
            artifact_hash="",
            session_hash_override=resolved_hash,
        ),
        None,
    )


def _collect_job_lineage(
    *,
    job_uid: str,
    roar_dir: Path,
    lineage_collector: LineageCollector,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    lineage = lineage_collector.collect_job(job_uid, roar_dir)
    if not lineage.jobs:
        return None, f"No local job matches '{job_uid}'."

    pipeline_id = lineage.pipeline.get("id") if isinstance(lineage.pipeline, dict) else None
    return (
        CollectedRegisterLineage(
            lineage=lineage,
            session_id=int(pipeline_id) if pipeline_id is not None else None,
            artifact_hash=select_representative_hash(lineage),
        ),
        None,
    )


def _collect_artifact_path_lineage(
    *,
    artifact_path: str,
    roar_dir: Path,
    cwd: Path,
    lineage_collector: LineageCollector,
    logger: ILogger,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    resolved_path = resolve_publish_artifact_path(artifact_path, cwd)
    if not resolved_path:
        return None, f"File not found: {artifact_path}"

    is_s3_artifact = _is_s3_url(resolved_path)
    if not is_s3_artifact and not os.path.exists(resolved_path):
        return None, f"File not found: {artifact_path}"

    with create_database_context(roar_dir) as db_ctx:
        if is_s3_artifact:
            db_artifact = db_ctx.artifacts.get_by_path(resolved_path)
            if not db_artifact:
                return None, _artifact_not_tracked_error(artifact_path)
            artifact_hash = select_publish_artifact_hash(db_artifact)
        else:
            artifact_hash = _compute_hash(resolved_path, logger=logger)
            if not artifact_hash:
                return None, f"Failed to compute hash for: {artifact_path}"
            db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash, algorithm="blake3")
            if not db_artifact:
                return None, _artifact_not_tracked_error(artifact_path)

        if not artifact_hash:
            return None, f"Artifact has no registered hash: {artifact_path}"

        logger.debug("Artifact hash: %s", artifact_hash[:12])

        session = db_ctx.sessions.get_active()
        if not session:
            return None, "No active session. Run 'roar run' to create a session first."

        logger.debug("Active session: %d", session["id"])
        lineage = lineage_collector.collect([artifact_hash], roar_dir)

    return (
        CollectedRegisterLineage(
            lineage=lineage,
            session_id=int(session["id"]),
            artifact_hash=artifact_hash,
        ),
        None,
    )


def _collect_artifact_hash_lineage(
    *,
    artifact_hash: str,
    roar_dir: Path,
    lineage_collector: LineageCollector,
) -> tuple[CollectedRegisterLineage | None, str | None]:
    with create_database_context(roar_dir) as db_ctx:
        db_artifact = db_ctx.artifacts.get_by_prefix(artifact_hash)
        if not db_artifact:
            return None, f"No tracked local artifact matches '{artifact_hash}'."

        resolved_hash = select_publish_artifact_hash(db_artifact)
        if not resolved_hash:
            return None, f"Artifact has no registered hash: {artifact_hash}"

        session = db_ctx.sessions.get_active()
        if not session:
            return None, "No active session. Run 'roar run' to create a session first."

        lineage = lineage_collector.collect([resolved_hash], roar_dir)

    return (
        CollectedRegisterLineage(
            lineage=lineage,
            session_id=int(session["id"]),
            artifact_hash=resolved_hash,
        ),
        None,
    )


def resolve_local_session_target(
    *,
    db_ctx,
    roar_dir: Path,
    session_hash: str,
    session_service: PublishSessionService,
) -> tuple[dict | None, str | None, str | None]:
    """Resolve a local session hash or prefix to a concrete local session."""
    candidates: list[tuple[dict, str]] = []
    for session in db_ctx.sessions.get_all():
        resolved_hash = session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=int(session["id"]),
        )
        if resolved_hash.startswith(session_hash):
            candidates.append((session, resolved_hash))

    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], None
    if len(candidates) > 1:
        return (
            None,
            None,
            (
                f"Ambiguous session hash prefix '{session_hash}'. "
                "Provide more characters to select a single local session."
            ),
        )

    local_session = db_ctx.sessions.get_by_hash_prefix(session_hash)
    if local_session:
        resolved_hash = session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=int(local_session["id"]),
        )
        return local_session, resolved_hash, None

    return None, None, f"No local session matches '{session_hash}'."


def select_representative_hash(lineage: LineageData) -> str:
    """Choose the single visible artifact hash for lineage results when available."""
    hashes = sorted(str(hash_value) for hash_value in lineage.artifact_hashes if hash_value)
    if len(hashes) == 1:
        return hashes[0]
    return ""


def _artifact_not_tracked_error(artifact_path: str) -> str:
    return (
        f"Artifact not tracked by roar: {artifact_path}\n"
        "Run 'roar run' to track this artifact first."
    )


def _compute_hash(path: str, *, logger: ILogger) -> str | None:
    try:
        hashes_by_path = compute_hashes_batch([path], ["blake3"])
        return hashes_by_path.get(path, {}).get("blake3")
    except (OSError, ValueError) as exc:
        logger.error("Failed to hash file %s: %s", path, exc)
        return None


def _is_s3_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme == "s3" and bool(parsed.netloc)
