"""Target classification helpers for publish registration workflows."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ...db.context import create_database_context

_STEP_REFERENCE_RE = re.compile(r"^@(?:B)?\d+$", re.IGNORECASE)
_SESSION_HASH_RE = re.compile(r"^[a-f0-9]{8,64}$")
_HEX_IDENTIFIER_RE = re.compile(r"^[a-f0-9]{4,64}$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedRegisterTarget:
    """Normalized target used to dispatch publish registration workflows."""

    kind: str
    value: str


def resolve_register_lineage_target(
    target: str | None,
    *,
    cwd: Path,
    roar_dir: Path,
) -> ResolvedRegisterTarget:
    """Resolve a CLI register target into the workflow it should trigger."""
    normalized_target = target.strip() if isinstance(target, str) else ""
    if not normalized_target:
        return ResolvedRegisterTarget(kind="active_session", value="")
    if is_register_step_reference(normalized_target):
        return ResolvedRegisterTarget(kind="step_reference", value=normalized_target)

    resolved_path = resolve_publish_artifact_path(normalized_target, cwd)
    if resolved_path and (_is_s3_url(normalized_target) or os.path.exists(resolved_path)):
        return ResolvedRegisterTarget(kind="artifact_path", value=normalized_target)

    resolved_job_uid = resolve_local_job_uid(normalized_target, roar_dir)
    if resolved_job_uid is not None:
        return ResolvedRegisterTarget(kind="job_uid", value=resolved_job_uid)

    resolved_artifact_hash = resolve_local_artifact_hash(normalized_target, roar_dir)
    if resolved_artifact_hash is not None:
        return ResolvedRegisterTarget(kind="artifact_hash", value=resolved_artifact_hash)

    if looks_like_session_hash(normalized_target):
        return ResolvedRegisterTarget(kind="session_hash", value=normalized_target)

    return ResolvedRegisterTarget(kind="artifact_path", value=normalized_target)


def parse_register_step_reference(reference: str) -> tuple[int, bool] | None:
    """Parse a local DAG step reference like `@4` or `@B2`."""
    if not is_register_step_reference(reference):
        return None
    step_ref = reference[1:]
    is_build = step_ref.upper().startswith("B")
    if is_build:
        step_ref = step_ref[1:]
    if not step_ref.isdigit():
        return None
    return int(step_ref), is_build


def resolve_publish_artifact_path(path: str, cwd: Path) -> str | None:
    """Resolve an artifact target to an absolute local path or passthrough URL."""
    if os.path.isabs(path):
        return path
    if _is_s3_url(path):
        return path
    return str(cwd / path)


def resolve_local_job_uid(target: str, roar_dir: Path) -> str | None:
    """Resolve a local job UID/prefix to its canonical job UID."""
    if not looks_like_hex_identifier(target) or len(target) > 12:
        return None

    with create_database_context(roar_dir) as db_ctx:
        job = db_ctx.jobs.get_by_uid(target)
    if not job:
        return None

    job_uid = job.get("job_uid")
    return str(job_uid) if isinstance(job_uid, str) and job_uid else None


def resolve_local_artifact_hash(target: str, roar_dir: Path) -> str | None:
    """Resolve a local artifact hash/prefix to its canonical hash."""
    if not looks_like_hex_identifier(target) or len(target) < 64:
        return None

    with create_database_context(roar_dir) as db_ctx:
        artifact = db_ctx.artifacts.get_by_prefix(target)
    if not artifact:
        return None

    return select_publish_artifact_hash(artifact)


def is_register_step_reference(target: str) -> bool:
    return bool(_STEP_REFERENCE_RE.match(target))


def looks_like_session_hash(target: str) -> bool:
    return bool(_SESSION_HASH_RE.match(target))


def looks_like_hex_identifier(target: str) -> bool:
    return bool(_HEX_IDENTIFIER_RE.match(target))


def select_publish_artifact_hash(artifact: dict) -> str | None:
    hashes = artifact.get("hashes")
    if not isinstance(hashes, list):
        return None

    best_priority = 10
    best_digest: str | None = None

    for entry in hashes:
        if not isinstance(entry, dict):
            continue

        algorithm = str(entry.get("algorithm") or "").strip().lower()
        digest = str(entry.get("digest") or "").strip()
        if not algorithm or not digest:
            continue

        priority = 0 if algorithm == "blake3" else 1 if algorithm == "etag" else 2
        if priority < best_priority:
            best_priority = priority
            best_digest = digest

    return best_digest


def _is_s3_url(path: str) -> bool:
    parsed = urlparse(path)
    return parsed.scheme.lower() == "s3"
