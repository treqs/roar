"""Shared publish registration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import BatchRegistrationResult, GitContext
from ...glaas_client import GlaasClient
from ...services.labels import collect_label_sync_payloads

_VALID_REMOTE_SOURCE_TYPES = {"s3", "gs", "https"}


@dataclass(frozen=True)
class CompositeRegistrationCandidate:
    """Prepared composite payload ready for GLaaS registration."""

    hash: str
    root_path: str
    component_count_total: int
    component_count_stored: int
    payload: dict[str, Any]


def normalize_registration_hashes(
    artifact: dict[str, Any],
    *,
    fallback_to_hash: bool = False,
) -> list[dict[str, str]]:
    """Normalize artifact hashes for GLaaS registration payloads."""
    normalized_hashes: list[dict[str, str]] = []
    seen: set[str] = set()

    raw_hashes = artifact.get("hashes")
    if isinstance(raw_hashes, list):
        for entry in raw_hashes:
            if not isinstance(entry, dict):
                continue
            algorithm = entry.get("algorithm")
            digest = entry.get("digest")
            if not isinstance(algorithm, str) or not isinstance(digest, str):
                continue
            algorithm_value = algorithm.strip().lower()
            digest_value = digest.strip().lower()
            if not algorithm_value or not digest_value:
                continue
            key = f"{algorithm_value}:{digest_value}"
            if key in seen:
                continue
            seen.add(key)
            normalized_hashes.append(
                {
                    "algorithm": algorithm_value,
                    "digest": digest_value,
                }
            )

    if not normalized_hashes and fallback_to_hash:
        hash_value = artifact.get("hash")
        if isinstance(hash_value, str) and hash_value.strip():
            algorithm = (
                "composite-blake3"
                if str(artifact.get("kind") or "").strip().lower() == "composite"
                else "blake3"
            )
            normalized_hashes.append(
                {
                    "algorithm": algorithm,
                    "digest": hash_value.strip().lower(),
                }
            )

    return normalized_hashes


def prepare_batch_registration_artifacts(
    artifacts: list[dict[str, Any]],
    session_hash: str,
    *,
    fallback_to_hash: bool = False,
    prefer_blake3_first: bool = False,
) -> list[dict[str, Any]]:
    """Prepare artifact payloads for the GLaaS batch registration endpoint."""
    prepared: list[dict[str, Any]] = []

    for artifact in artifacts:
        hashes = normalize_registration_hashes(artifact, fallback_to_hash=fallback_to_hash)
        if not hashes:
            continue

        if prefer_blake3_first:
            blake3_hashes = [h for h in hashes if h["algorithm"] == "blake3"]
            other_hashes = [h for h in hashes if h["algorithm"] != "blake3"]
            hashes = blake3_hashes + other_hashes

        if artifact.get("kind") == "composite" or any(
            h.get("algorithm") == "composite-blake3" for h in hashes
        ):
            continue

        try:
            size = max(0, int(artifact.get("size", 0)))
        except (TypeError, ValueError):
            size = 0

        prepared.append(
            {
                "hashes": hashes,
                "size": size,
                "source_type": normalize_registration_source_type(artifact.get("source_type")),
                "session_hash": session_hash,
            }
        )

    return prepared


def register_publish_lineage(
    *,
    coordinator: Any,
    glaas_client: GlaasClient,
    session_hash: str,
    git_context: GitContext,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    db_ctx: Any | None,
    session_id: int | None,
    label_artifacts: list[dict[str, Any]],
    pre_registration_errors: list[str] | None = None,
) -> BatchRegistrationResult:
    """Run the shared publish registration phase and optional label sync."""
    batch_result: BatchRegistrationResult = coordinator.register_lineage(
        session_hash=session_hash,
        git_context=git_context,
        jobs=jobs,
        artifacts=artifacts,
    )

    if pre_registration_errors:
        batch_result.errors = [*pre_registration_errors, *batch_result.errors]

    if session_id is not None and db_ctx is not None:
        sync_publish_labels(
            glaas_client=glaas_client,
            db_ctx=db_ctx,
            session_id=session_id,
            session_hash=session_hash,
            jobs=jobs,
            artifacts=label_artifacts,
            errors=batch_result.errors,
        )

    return batch_result


def preregister_lineage_composites(
    *,
    glaas_client: GlaasClient,
    payloads: list[CompositeRegistrationCandidate],
    registration_errors: list[str],
    logger: ILogger,
) -> list[dict[str, Any]]:
    """Register lineage composites before the main link phase."""
    registrations: list[dict[str, Any]] = []

    for item in payloads:
        response = glaas_client.register_composite_artifact(item.payload)
        result, error = parse_composite_registration_response(response)

        registration: dict[str, Any] = {
            "lineage": True,
            "hash": item.hash,
            "root_path": item.root_path,
            "component_count_total": item.component_count_total,
            "component_count_stored": item.component_count_stored,
        }
        if error:
            registration["registered"] = False
            registration["error"] = error
            registration_errors.append(f"Lineage composite {item.hash[:12]}: {error}")
        else:
            registration["registered"] = True
            if isinstance(result, dict):
                if "artifact_id" in result:
                    registration["artifact_id"] = result["artifact_id"]
                if "created" in result:
                    registration["created"] = result["created"]

        registrations.append(registration)

    if registrations:
        failures = sum(1 for item in registrations if not item.get("registered"))
        logger.debug(
            "Lineage composite pre-registration complete: %d total, %d failed",
            len(registrations),
            failures,
        )

    return registrations


def sync_publish_labels(
    *,
    glaas_client: GlaasClient,
    db_ctx: Any,
    session_id: int,
    session_hash: str,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    errors: list[str] | None = None,
) -> None:
    """Sync publish labels to GLaaS and record any error on the supplied list."""
    payloads = collect_label_sync_payloads(
        db_ctx,
        session_id=session_id,
        session_hash=session_hash,
        jobs=jobs,
        artifacts=artifacts,
    )
    if not payloads:
        return

    _label_result, label_error = glaas_client.sync_labels(payloads)
    if label_error and errors is not None:
        errors.append(f"Label sync failed: {label_error}")


def extract_composite_digest(hashes: list[dict[str, str]]) -> str | None:
    """Return the composite digest from a normalized hash list."""
    for item in hashes:
        if item.get("algorithm") == "composite-blake3":
            digest = item.get("digest")
            if isinstance(digest, str) and digest:
                return digest
    return None


def ensure_composite_hash_entry(
    hashes: list[dict[str, str]],
    composite_digest: str,
) -> list[dict[str, str]]:
    """Ensure the composite digest is present in the outgoing hash list."""
    normalized_hashes = [dict(item) for item in hashes]
    has_composite_digest = any(
        item.get("algorithm") == "composite-blake3" and item.get("digest") == composite_digest
        for item in normalized_hashes
    )
    if not has_composite_digest:
        normalized_hashes.insert(
            0,
            {"algorithm": "composite-blake3", "digest": composite_digest},
        )
    return normalized_hashes


def normalize_registration_source_type(source_type: Any) -> str | None:
    """Normalize a source type to the remote GLaaS registration contract."""
    if not isinstance(source_type, str):
        return None
    normalized = source_type.strip().lower()
    if normalized in _VALID_REMOTE_SOURCE_TYPES:
        return normalized
    return None


def parse_composite_registration_response(
    response: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize the GLaaS composite registration response contract."""
    if isinstance(response, tuple) and len(response) == 2:
        raw_result, raw_error = response
        if isinstance(raw_error, str):
            return (
                raw_result if isinstance(raw_result, dict) else None,
                raw_error or None,
            )
        if raw_error is not None:
            return None, str(raw_error)
        if not isinstance(raw_result, dict):
            return (
                None,
                "Unexpected response from GLaaS when registering composite artifact: "
                f"expected dict payload, got {type(raw_result).__name__}",
            )
        return raw_result, None

    if response is None:
        return None, "Empty response from GLaaS when registering composite artifact"

    return None, "Unexpected response from GLaaS when registering composite artifact"
