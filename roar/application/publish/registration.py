"""Shared publish registration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.digests import is_composite_algorithm
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import BatchRegistrationResult, GitContext
from ..labels import collect_label_sync_payloads, count_user_labels
from .remote_registry import RemoteRegistryTransport, coerce_remote_registry

_VALID_REMOTE_SOURCE_TYPES = {"s3", "gs", "https", "hf"}


@dataclass(frozen=True)
class CompositeRegistrationCandidate:
    """Prepared composite payload ready for GLaaS registration."""

    hash: str
    root_path: str
    component_count_total: int
    component_count_stored: int
    payload: dict[str, Any]


def normalize_lineage_component_rows(
    component_rows: list[dict[str, Any]],
    *,
    resolve_component: Any,
    logger: ILogger,
) -> list[dict[str, Any]]:
    """Normalize raw local composite-component rows for registration."""
    components: list[dict[str, Any]] = []

    for row in component_rows:
        relative_path = row.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            continue

        resolved_component = resolve_component(row)
        if resolved_component is None:
            logger.warning(
                "Skipping lineage composite component %s: missing resolvable blake3 digest",
                relative_path,
            )
            continue
        component_algorithm, digest = resolved_component

        leaf_kind = row.get("leaf_kind")
        if not isinstance(leaf_kind, str) or not leaf_kind:
            leaf_kind = "file"

        component_size = _normalize_component_size(row.get("component_size"))

        component_type = row.get("component_type")
        if component_type is not None and not isinstance(component_type, str):
            component_type = None

        components.append(
            {
                "relative_path": relative_path,
                "leaf_kind": leaf_kind,
                "component_algorithm": component_algorithm,
                "component_digest": digest.lower(),
                "component_size": component_size,
                "component_type": component_type,
            }
        )

    return components


def build_lineage_composite_candidate(
    *,
    artifact: dict[str, Any],
    composite_digest: str,
    hashes: list[dict[str, str]],
    components: list[dict[str, Any]],
    membership_index: dict[str, Any] | None,
    session_hash: str,
    composite_builder: Any,
    metadata: str | None = None,
    logger: ILogger,
) -> CompositeRegistrationCandidate | None:
    """Build a composite registration candidate from normalized lineage data."""
    component_count_total = resolve_lineage_component_count_total(
        artifact_component_count=artifact.get("component_count"),
        membership_index=membership_index,
        stored_components=len(components),
    )
    if component_count_total <= 0:
        logger.warning(
            "Skipping lineage composite %s: missing component_count metadata",
            composite_digest[:12],
        )
        return None

    membership_payload = build_lineage_membership_index_payload(
        composite_builder=composite_builder,
        membership_index=membership_index,
        component_count_total=component_count_total,
        components=components,
    )
    normalized_hashes = ensure_composite_hash_entry(hashes, composite_digest)
    root_path = _artifact_path(artifact) or ""
    source_type = normalize_registration_source_type(artifact.get("source_type"))
    try:
        size = max(0, int(artifact.get("size", 0)))
    except (TypeError, ValueError):
        size = 0

    payload: dict[str, Any] = {
        "hash": composite_digest,
        "hashes": normalized_hashes,
        "size": size,
        "source_type": source_type,
        "session_hash": session_hash,
        "component_count_total": component_count_total,
        "components": components,
        "membership_index": membership_payload,
    }
    if metadata is not None:
        payload["metadata"] = metadata

    # The local component set is complete (so prune/membership resolution is exact); cap
    # the *upload* copy to bound the GLaaS payload + paid storage. The membership bloom
    # (over all leaves) and component_count_total survive the cap.
    payload = composite_builder.cap_payload_for_upload(payload)

    return CompositeRegistrationCandidate(
        hash=composite_digest,
        root_path=str(root_path),
        component_count_total=component_count_total,
        component_count_stored=len(payload.get("components") or []),
        payload=payload,
    )


def resolve_lineage_component_count_total(
    artifact_component_count: Any,
    membership_index: dict[str, Any] | None,
    stored_components: int,
) -> int:
    """Resolve the authoritative component count for a lineage composite."""
    try:
        artifact_total = int(artifact_component_count)
    except (TypeError, ValueError):
        artifact_total = 0

    membership_total = 0
    if isinstance(membership_index, dict):
        try:
            membership_total = int(membership_index.get("total_components", 0))
        except (TypeError, ValueError):
            membership_total = 0

    return max(artifact_total, membership_total, stored_components)


def build_lineage_membership_index_payload(
    *,
    composite_builder: Any,
    membership_index: dict[str, Any] | None,
    component_count_total: int,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the membership_index payload for a lineage composite registration."""
    stored_components = len(components)
    payload: dict[str, Any] = {
        "total_components": component_count_total,
        "stored_components": stored_components,
    }

    if stored_components == component_count_total and stored_components > 0:
        from .composite_builder import CompositeLeaf

        leaves: list[CompositeLeaf] = []
        for component in components:
            digest = component.get("component_digest")
            if not isinstance(digest, str) or not digest:
                continue
            component_type_raw = component.get("component_type")
            component_type = component_type_raw if isinstance(component_type_raw, str) else None
            leaf_kind_raw = component.get("leaf_kind")
            leaf_kind = leaf_kind_raw if isinstance(leaf_kind_raw, str) else "file"
            algorithm_raw = component.get("component_algorithm")
            algorithm = (
                algorithm_raw if isinstance(algorithm_raw, str) and algorithm_raw else "blake3"
            )
            leaves.append(
                CompositeLeaf(
                    relative_path=str(component.get("relative_path") or ""),
                    digest=digest.lower(),
                    size=_normalize_component_size(component.get("component_size")),
                    component_type=component_type,
                    leaf_kind=leaf_kind,
                    component_algorithm=algorithm,
                )
            )

        if len(leaves) == stored_components:
            bloom_payload = composite_builder._build_membership_index_base(leaves)
            payload["bloom_filter_base64"] = bloom_payload.get("bloom_filter_base64")
            payload["bloom_bits"] = bloom_payload.get("bloom_bits")
            payload["bloom_hashes"] = bloom_payload.get("bloom_hashes")
            payload["bloom_version"] = bloom_payload.get("bloom_version")
            return payload

    for key in ("bloom_filter_base64", "bloom_bits", "bloom_hashes", "bloom_version"):
        value = membership_index.get(key) if isinstance(membership_index, dict) else None
        if value is None:
            continue
        if key in {"bloom_bits", "bloom_hashes", "bloom_version"}:
            try:
                payload[key] = int(value)
            except (TypeError, ValueError):
                continue
            continue
        payload[key] = value

    required_bloom_keys = (
        "bloom_filter_base64",
        "bloom_bits",
        "bloom_hashes",
        "bloom_version",
    )
    if not all(key in payload and payload[key] is not None for key in required_bloom_keys):
        raise ValueError(
            "Lineage composite membership_index is missing required bloom fields; "
            "cannot register composite without a complete membership bloom index."
        )

    return payload


def _artifact_path(artifact: dict[str, Any]) -> str | None:
    for key in ("first_seen_path", "path"):
        value = artifact.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
            is_composite_algorithm(h.get("algorithm")) for h in hashes
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
    remote_registry: RemoteRegistryTransport | None = None,
    glaas_client: Any | None = None,
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

    resolved_remote_registry = coerce_remote_registry(
        remote_registry=remote_registry,
        glaas_client=glaas_client,
    )

    labels_are_safe_to_sync = batch_result.jobs_failed == 0 and batch_result.artifacts_failed == 0

    if session_id is not None and db_ctx is not None and labels_are_safe_to_sync:
        batch_result.labels_synced = sync_publish_labels(
            remote_registry=resolved_remote_registry,
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
    remote_registry: RemoteRegistryTransport | None = None,
    glaas_client: Any | None = None,
    payloads: list[CompositeRegistrationCandidate],
    registration_errors: list[str],
    logger: ILogger,
) -> list[dict[str, Any]]:
    """Register lineage composites before the main link phase."""
    registrations: list[dict[str, Any]] = []
    resolved_remote_registry = coerce_remote_registry(
        remote_registry=remote_registry,
        glaas_client=glaas_client,
    )

    for item in payloads:
        response = resolved_remote_registry.register_composite_artifact(item.payload)
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
    remote_registry: RemoteRegistryTransport | None = None,
    glaas_client: Any | None = None,
    db_ctx: Any,
    session_id: int | None,
    session_hash: str,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    errors: list[str] | None = None,
) -> int:
    """Sync current local labels for published entities to GLaaS.

    Returns the number of user-set labels synced (non-``roar.*`` label keys
    across the published entities); ``0`` if nothing was synced or the sync
    failed.
    """
    payloads = collect_label_sync_payloads(
        db_ctx,
        session_id=session_id,
        session_hash=session_hash,
        jobs=jobs,
        artifacts=artifacts,
    )
    if not payloads:
        return 0

    resolved_remote_registry = coerce_remote_registry(
        remote_registry=remote_registry,
        glaas_client=glaas_client,
    )

    _label_result, label_error = resolved_remote_registry.sync_labels(payloads)
    if label_error:
        if errors is not None:
            errors.append(f"Label sync failed: {label_error}")
        return 0

    return count_user_labels(payloads)


def extract_composite_digest(hashes: list[dict[str, str]]) -> str | None:
    """Return the composite digest from a normalized hash list (any composite-* algo)."""
    for item in hashes:
        if is_composite_algorithm(item.get("algorithm")):
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
        is_composite_algorithm(item.get("algorithm")) and item.get("digest") == composite_digest
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


def _normalize_component_size(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    return 0
