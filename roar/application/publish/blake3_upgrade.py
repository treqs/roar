"""Mechanical helpers for upgrading S3 etag lineage to blake3."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...db.context import create_database_context

_Blake3Constructor = Any

try:
    from blake3 import blake3 as _blake3_import
except Exception:
    _blake3_constructor: _Blake3Constructor | None = None
else:
    _blake3_constructor = _blake3_import

boto3 = None


def ensure_boto3() -> None:
    global boto3
    if boto3 is None:
        import boto3 as _boto3

        boto3 = _boto3


def upgrade_s3_etags_to_blake3(
    *,
    roar_dir: Path,
    lineage: LineageData,
    logger: ILogger,
) -> None:
    """Upgrade etag-only S3 lineage artifacts to include blake3 hashes."""
    if not lineage.artifacts:
        return

    if _blake3_constructor is None:
        logger.warning("Skipping --as-blake3 upgrade because the blake3 package is not installed.")
        return

    try:
        ensure_boto3()
    except Exception as exc:
        logger.warning("Skipping --as-blake3 upgrade because boto3 is unavailable: %s", exc)
        return

    with create_database_context(roar_dir) as db_ctx:
        for artifact in lineage.artifacts:
            if not needs_blake3_upgrade(artifact):
                continue

            digest = ensure_artifact_blake3_digest(
                db_ctx=db_ctx,
                artifact=artifact,
                logger=logger,
            )
            if digest:
                lineage.artifact_hashes.add(digest)


def needs_blake3_upgrade(artifact: dict[str, Any]) -> bool:
    """Return whether an artifact needs a blake3 hash row added."""
    hashes = artifact.get("hashes")
    if not isinstance(hashes, list):
        return False

    has_etag = False
    has_blake3 = False
    for entry in hashes:
        if not isinstance(entry, dict):
            continue
        algorithm = entry.get("algorithm")
        if not isinstance(algorithm, str):
            continue
        normalized = algorithm.strip().lower()
        if normalized == "etag":
            has_etag = True
        elif normalized == "blake3":
            has_blake3 = True

    return has_etag and not has_blake3


def select_hash_by_algorithm(artifact: dict[str, Any], algorithm: str) -> str | None:
    """Return a normalized digest for the requested algorithm."""
    hashes = artifact.get("hashes")
    if not isinstance(hashes, list):
        return None

    target = algorithm.strip().lower()
    for entry in hashes:
        if not isinstance(entry, dict):
            continue
        current_algorithm = entry.get("algorithm")
        digest = entry.get("digest")
        if (
            isinstance(current_algorithm, str)
            and current_algorithm.strip().lower() == target
            and isinstance(digest, str)
            and digest
        ):
            return digest.lower()

    return None


def extract_s3_url(artifact: dict[str, Any]) -> str | None:
    """Extract the first S3 source URL from an artifact-like dict."""
    for key in ("source_url", "first_seen_path", "path"):
        value = artifact.get(key)
        if isinstance(value, str) and value.startswith("s3://"):
            return value
    return None


def parse_s3_url(s3_url: str) -> tuple[str, str] | None:
    """Parse an S3 URL into bucket/key."""
    parsed = urlparse(s3_url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if parsed.scheme != "s3" or not bucket or not key:
        return None
    return bucket, key


def compute_s3_blake3_digest(
    *,
    s3_client: Any,
    bucket: str,
    key: str,
    logger: ILogger,
) -> str | None:
    """Compute a blake3 digest for an S3 object."""
    if _blake3_constructor is None:
        return None

    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response.get("Body")
        if body is None:
            return None

        hasher = _blake3_constructor()
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(bytes(chunk))
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        return hasher.hexdigest()
    except Exception as exc:
        logger.warning("Failed to compute blake3 for s3://%s/%s: %s", bucket, key, exc)
        return None


def attach_blake3_to_artifact(artifact: dict[str, Any], digest: str) -> None:
    """Attach a blake3 hash row to an in-memory artifact dict."""
    hashes = artifact.get("hashes")
    if not isinstance(hashes, list):
        hashes = []
        artifact["hashes"] = hashes

    for entry in hashes:
        if not isinstance(entry, dict):
            continue
        if entry.get("algorithm") == "blake3" and entry.get("digest") == digest:
            artifact["hash"] = digest
            return

    hashes.append({"algorithm": "blake3", "digest": digest})
    artifact["hash"] = digest


def ensure_artifact_blake3_digest(
    *,
    db_ctx: Any,
    artifact: dict[str, Any],
    logger: ILogger,
) -> str | None:
    """Ensure an artifact has a stored blake3 digest, adding one if needed."""
    existing_digest = select_hash_by_algorithm(artifact, "blake3")
    if existing_digest is not None:
        return existing_digest

    if not needs_blake3_upgrade(artifact):
        return None

    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return None

    s3_url = extract_s3_url(artifact)
    if not s3_url:
        return None

    parsed = parse_s3_url(s3_url)
    if parsed is None:
        return None
    bucket, key = parsed

    try:
        ensure_boto3()
    except Exception as exc:
        logger.warning(
            "Skipping blake3 upgrade for %s because boto3 is unavailable: %s", s3_url, exc
        )
        return None

    assert boto3 is not None
    digest = compute_s3_blake3_digest(
        s3_client=boto3.client("s3"),
        bucket=bucket,
        key=key,
        logger=logger,
    )
    if not digest:
        return None

    db_ctx.session.execute(
        text(
            """
            INSERT OR IGNORE INTO artifact_hashes (artifact_id, algorithm, digest)
            VALUES (:artifact_id, 'blake3', :digest)
            """
        ),
        {"artifact_id": artifact_id, "digest": digest},
    )

    has_blake3_row = db_ctx.session.execute(
        text(
            """
            SELECT 1
            FROM artifact_hashes
            WHERE artifact_id = :artifact_id
              AND algorithm = 'blake3'
              AND digest = :digest
            LIMIT 1
            """
        ),
        {"artifact_id": artifact_id, "digest": digest},
    ).scalar_one_or_none()
    if not has_blake3_row:
        return None

    attach_blake3_to_artifact(artifact, digest)
    return digest
