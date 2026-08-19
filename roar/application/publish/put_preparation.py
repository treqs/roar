"""Application-owned preparation for put workflows."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import text

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...db.hashing import hash_files_blake3
from ...integrations.glaas import GlaasClient
from ..git import resolve_roar_git_context
from .datasets import (
    detect_additional_publish_composite_roots,
    infer_publish_dataset_identifiers,
)
from .remote_registry import coerce_remote_registry
from .runtime import PublishRuntime
from .session import prepare_publish_session

if TYPE_CHECKING:
    from .source_resolution import ResolvedSource


@dataclass(frozen=True)
class DelegatedPutOperation:
    """Durable local reservation for one broker-backed put operation."""

    task_identity: str
    session_id: int
    ordinal: int
    request_fingerprint: str
    put_job_uid: str


@dataclass(frozen=True)
class PreparedPutExecution:
    """Application-prepared context for a put execution."""

    glaas_client: GlaasClient
    session_id: int
    session_hash: str
    session_url: str | None
    git_context: GitContext
    resolved_sources: list[ResolvedSource]
    destination_type: str
    composite_source_type: str | None
    source_hashes: dict[str, str] = field(default_factory=dict)
    registration_session_id: str | None = None
    registration_session_mode: str | None = None
    registration_session_status: str | None = None
    dataset_identifiers: list[dict[str, Any]] = field(default_factory=list)
    additional_composite_roots: dict[Path, list[ResolvedSource]] = field(default_factory=dict)
    delegated_put_operation: DelegatedPutOperation | None = None
    lineage: LineageData | None = None


def prepare_put_execution(
    *,
    db_ctx,
    runtime: PublishRuntime,
    roar_dir: Path,
    repo_root: Path,
    sources: list[str],
    destination: str,
    git_commit: str | None,
    logger: ILogger,
    operation_options: Mapping[str, Any] | None = None,
) -> PreparedPutExecution:
    """Resolve the local context needed to execute a put workflow."""
    from .source_resolution import SourceResolver

    active_session = db_ctx.sessions.get_active()
    if active_session is None:
        raise ValueError("No active session")

    session_id = int(active_session["id"])
    from ...integrations.config import config_get

    git_context = resolve_roar_git_context(
        repo_root,
        logger=logger,
        git_commit=git_commit,
        configured_remote=config_get("git.remote"),
    )
    runtime_dict = getattr(runtime, "__dict__", {})
    remote_registry = coerce_remote_registry(
        remote_registry=runtime_dict.get("remote_registry"),
        glaas_client=runtime.glaas_client,
        session_service=runtime.session_service,
        registration_coordinator=runtime_dict.get("registration_coordinator"),
    )
    resolver = SourceResolver(
        repo_root=repo_root,
        session_repo=db_ctx.sessions,
        job_repo=db_ctx.jobs,
    )
    resolved_sources = resolver.resolve(sources)
    source_hashes = hash_files_blake3([source.path for source in resolved_sources])
    missing_hashes = [
        str(source.path) for source in resolved_sources if str(source.path) not in source_hashes
    ]
    if missing_hashes:
        raise OSError(f"Failed to hash put source: {missing_hashes[0]}")

    operation_payload: dict[str, Any] = {
        "destination": destination,
        "git": {
            "branch": git_context.branch,
            "commit": git_context.commit,
            "repo": git_context.repo,
        },
        "local_session_hash": runtime.session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session_id,
        ),
        "local_session_id": session_id,
        "options": dict(operation_options or {}),
        "sources": sorted(
            [
                {
                    "digest": source_hashes[str(source.path)],
                    "path": os.path.relpath(source.path.resolve(), repo_root.resolve()),
                    "relative_key": source.relative_key,
                    "size": source.path.stat().st_size,
                }
                for source in resolved_sources
            ],
            key=lambda source: (source["path"], source["relative_key"]),
        ),
    }
    delegated_task_identity = _delegated_task_identity()
    collected_lineage: LineageData | None = None
    if delegated_task_identity is not None:
        pending_put_job_uid = _pending_put_job_uid(
            db_ctx,
            delegated_task_identity,
            session_id,
        )
        from .lineage import LineageCollector

        collected_lineage = LineageCollector().collect(
            sorted(set(source_hashes.values())),
            roar_dir,
        )
        collected_lineage = _without_lineage_job(collected_lineage, pending_put_job_uid)
        operation_payload["lineage_revision"] = _collected_lineage_revision(
            collected_lineage,
        )
    request_fingerprint = _fingerprint(operation_payload)
    delegated_put_operation = _reserve_delegated_put_operation(
        db_ctx=db_ctx,
        delegated_task_identity=delegated_task_identity,
        session_id=session_id,
        request_fingerprint=request_fingerprint,
    )
    operation_fingerprint = (
        _fingerprint(
            {
                "ordinal": delegated_put_operation.ordinal,
                "request_fingerprint": request_fingerprint,
            }
        )
        if delegated_put_operation is not None
        else request_fingerprint
    )

    publish_session = prepare_publish_session(
        remote_registry=remote_registry,
        roar_dir=roar_dir,
        session_id=session_id,
        git_context=git_context,
        logger=logger,
        register_with_glaas=True,
        operation_kind="put",
        operation_fingerprint=operation_fingerprint,
    )
    dataset_identifiers = infer_publish_dataset_identifiers(
        repo_root=repo_root,
        source_specs=sources,
        resolved_sources=resolved_sources,
    )
    additional_composite_roots = detect_additional_publish_composite_roots(
        resolved_sources=resolved_sources,
    )

    destination_type = _destination_type(destination)
    composite_source_type = (
        destination_type if destination_type in {"s3", "gs", "https", "hf"} else None
    )

    return PreparedPutExecution(
        glaas_client=runtime.glaas_client,
        session_id=session_id,
        session_hash=publish_session.session_hash,
        session_url=publish_session.session_url,
        git_context=git_context,
        registration_session_id=publish_session.registration_session_id,
        registration_session_mode=publish_session.registration_session_mode,
        registration_session_status=(
            publish_session.registration_session_status
            if isinstance(publish_session.registration_session_status, str)
            else None
        ),
        resolved_sources=resolved_sources,
        destination_type=destination_type,
        composite_source_type=composite_source_type,
        source_hashes=source_hashes,
        dataset_identifiers=dataset_identifiers,
        additional_composite_roots=additional_composite_roots,
        delegated_put_operation=delegated_put_operation,
        lineage=collected_lineage,
    )


def complete_delegated_put_operation(
    db_ctx: Any,
    operation: DelegatedPutOperation | None,
) -> None:
    """Mark a broker-backed put complete after its local and remote writes succeed."""
    if not isinstance(operation, DelegatedPutOperation):
        return

    completed_at = time.time()
    result = db_ctx.session.execute(
        text(
            """
            UPDATE delegated_put_operations
            SET status = 'completed', updated_at = :completed_at, completed_at = :completed_at
            WHERE task_identity = :task_identity
              AND session_id = :session_id
              AND ordinal = :ordinal
              AND request_fingerprint = :request_fingerprint
              AND status = 'pending'
            """
        ),
        {
            "completed_at": completed_at,
            "task_identity": operation.task_identity,
            "session_id": operation.session_id,
            "ordinal": operation.ordinal,
            "request_fingerprint": operation.request_fingerprint,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError("Delegated put operation reservation changed before completion")
    db_ctx.commit()


def _delegated_task_identity() -> str | None:
    values = [
        os.environ.get("ROAR_DELEGATED_JOB_ID", "").strip(),
        os.environ.get("ROAR_DELEGATED_EXECUTION_ATTEMPT_ID", "").strip(),
        os.environ.get("ROAR_DELEGATED_TASK_ID", "").strip(),
    ]
    if not any(values):
        return None
    if not all(values):
        raise ValueError("Delegated publication task identity is incomplete")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _pending_put_job_uid(db_ctx: Any, task_identity: str, session_id: int) -> str | None:
    row = (
        db_ctx.session.execute(
            text(
                """
                SELECT put_job_uid
                FROM delegated_put_operations
                WHERE task_identity = :task_identity
                  AND session_id = :session_id
                  AND status = 'pending'
                """
            ),
            {"task_identity": task_identity, "session_id": session_id},
        )
        .mappings()
        .one_or_none()
    )
    return str(row["put_job_uid"]) if row and row["put_job_uid"] else None


def _without_lineage_job(lineage: LineageData, job_uid: str | None) -> LineageData:
    if not job_uid:
        return lineage
    return LineageData(
        jobs=[job for job in lineage.jobs if job.get("job_uid") != job_uid],
        artifacts=lineage.artifacts,
        artifact_hashes=lineage.artifact_hashes,
        pipeline=lineage.pipeline,
    )


def _collected_lineage_revision(lineage: LineageData) -> str:
    """Hash the exact target-rooted lineage snapshot passed to put execution."""
    jobs = sorted(
        (_stable_json_value(job) for job in lineage.jobs),
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    artifacts = sorted(
        (_stable_json_value(artifact) for artifact in lineage.artifacts),
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    return _fingerprint(
        {
            "jobs": jobs,
            "artifacts": artifacts,
            "artifact_hashes": sorted(lineage.artifact_hashes),
        }
    )


def _stable_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _stable_json_value(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(child) for child in value]
    if isinstance(value, set):
        normalized = [_stable_json_value(child) for child in value]
        return sorted(normalized, key=lambda child: json.dumps(child, sort_keys=True))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _reserve_delegated_put_operation(
    *,
    db_ctx: Any,
    delegated_task_identity: str | None,
    session_id: int,
    request_fingerprint: str,
) -> DelegatedPutOperation | None:
    if delegated_task_identity is None:
        return None

    now = time.time()
    candidate_put_job_uid = f"delegated-put-{secrets.token_hex(12)}"
    row = (
        db_ctx.session.execute(
            text(
                """
                INSERT INTO delegated_put_operations (
                    task_identity,
                    session_id,
                    ordinal,
                    request_fingerprint,
                    put_job_uid,
                    status,
                    created_at,
                    updated_at,
                    completed_at
                ) VALUES (
                    :task_identity,
                    :session_id,
                    1,
                    :request_fingerprint,
                    :put_job_uid,
                    'pending',
                    :now,
                    :now,
                    NULL
                )
                ON CONFLICT(task_identity, session_id) DO UPDATE SET
                    ordinal = CASE
                        WHEN delegated_put_operations.status = 'completed'
                        THEN delegated_put_operations.ordinal + 1
                        ELSE delegated_put_operations.ordinal
                    END,
                    request_fingerprint = CASE
                        WHEN delegated_put_operations.status = 'completed'
                        THEN excluded.request_fingerprint
                        ELSE delegated_put_operations.request_fingerprint
                    END,
                    put_job_uid = CASE
                        WHEN delegated_put_operations.status = 'completed'
                        THEN excluded.put_job_uid
                        ELSE delegated_put_operations.put_job_uid
                    END,
                    status = 'pending',
                    updated_at = excluded.updated_at,
                    completed_at = NULL
                WHERE delegated_put_operations.status = 'completed'
                   OR delegated_put_operations.request_fingerprint = excluded.request_fingerprint
                RETURNING ordinal, request_fingerprint, put_job_uid
                """
            ),
            {
                "task_identity": delegated_task_identity,
                "session_id": session_id,
                "request_fingerprint": request_fingerprint,
                "put_job_uid": candidate_put_job_uid,
                "now": now,
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError(
            "A different delegated put operation is already pending for this task; "
            "retry the original command"
        )
    db_ctx.commit()
    return DelegatedPutOperation(
        task_identity=delegated_task_identity,
        session_id=session_id,
        ordinal=int(row["ordinal"]),
        request_fingerprint=str(row["request_fingerprint"]),
        put_job_uid=str(row["put_job_uid"]),
    )


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _destination_type(destination: str) -> str:
    parsed = urlparse(destination)
    return parsed.scheme or "local"
