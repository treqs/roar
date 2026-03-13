"""Shared publish registration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.interfaces.logger import ILogger
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
