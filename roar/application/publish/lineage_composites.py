"""Mechanical helpers for lineage composite preregistration."""

from __future__ import annotations

import json
from typing import Any

from ...application.publish.registration import (
    CompositeRegistrationCandidate,
    build_lineage_composite_candidate,
    extract_composite_digest,
    normalize_lineage_component_rows,
    normalize_registration_hashes,
    preregister_lineage_composites,
)
from ...core.interfaces.logger import ILogger
from ...db.context import optional_repo
from ...integrations.glaas import GlaasClient
from .blake3_upgrade import ensure_artifact_blake3_digest, select_hash_by_algorithm


def _coerce_metadata_json(value: Any) -> str | None:
    """Normalize a stored artifact ``metadata`` value to a JSON string for GLaaS.

    The composite registration payload carries ``metadata`` as a JSON string; the
    local row may hold it already-serialized or as a dict (repo-dependent). Returns
    ``None`` for empty/unset metadata so the payload omits the field entirely.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return None
    return None


def has_lineage_composites(artifacts: list[dict[str, Any]]) -> bool:
    """Return whether any lineage artifact is a composite."""
    return any(
        extract_composite_digest(extract_registration_hashes(artifact)) for artifact in artifacts
    )


def preregister_lineage_composites_with_glaas(
    *,
    glaas_client: GlaasClient,
    db_ctx: Any,
    lineage_artifacts: list[dict[str, Any]],
    session_hash: str,
    registration_errors: list[str],
    composite_builder: Any,
    logger: ILogger,
    registration_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Prepare and preregister lineage composites before batch link resolution."""
    payloads = build_lineage_composite_payloads(
        db_ctx=db_ctx,
        lineage_artifacts=lineage_artifacts,
        session_hash=session_hash,
        composite_builder=composite_builder,
        logger=logger,
    )
    return preregister_lineage_composites(
        glaas_client=glaas_client,
        payloads=payloads,
        registration_errors=registration_errors,
        logger=logger,
        registration_session_id=registration_session_id,
    )


def build_lineage_composite_payloads(
    *,
    db_ctx: Any,
    lineage_artifacts: list[dict[str, Any]],
    session_hash: str,
    composite_builder: Any,
    logger: ILogger,
) -> list[CompositeRegistrationCandidate]:
    """Build preregistration payloads for lineage composites from local DB state."""
    composites_repo: Any = optional_repo(db_ctx, "composites")
    lineage_artifacts_by_id = {
        str(artifact_id): artifact
        for artifact in lineage_artifacts
        if isinstance((artifact_id := artifact.get("id")), str) and artifact_id
    }
    payloads: list[CompositeRegistrationCandidate] = []
    seen_hashes: set[str] = set()

    for artifact in lineage_artifacts:
        hashes = extract_registration_hashes(artifact)
        composite_digest = extract_composite_digest(hashes)
        if not composite_digest or composite_digest in seen_hashes:
            continue

        component_rows: list[dict[str, Any]] = []
        membership_index: dict[str, Any] | None = None
        artifact_id = artifact.get("id")
        # Lineage artifact dicts may omit source_type / metadata; pull them from the
        # local row so the composite payload records its origin (e.g. 'hf') and its
        # provenance metadata (HF coordinates, subset_of) reaches GLaaS.
        metadata = _coerce_metadata_json(artifact.get("metadata"))
        needs_source = not artifact.get("source_type")
        if (needs_source or metadata is None) and isinstance(artifact_id, str) and artifact_id:
            artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
            loaded = artifacts_repo.get(artifact_id) if artifacts_repo is not None else None
            if isinstance(loaded, dict):
                if needs_source and loaded.get("source_type"):
                    artifact = {**artifact, "source_type": loaded["source_type"]}
                if metadata is None:
                    metadata = _coerce_metadata_json(loaded.get("metadata"))
        if composites_repo is not None and isinstance(artifact_id, str) and artifact_id:
            rows = composites_repo.get_components(artifact_id, limit=5000)
            if isinstance(rows, list):
                component_rows = [row for row in rows if isinstance(row, dict)]

            raw_membership = composites_repo.get_membership_index(artifact_id)
            if isinstance(raw_membership, dict):
                membership_index = raw_membership

        components = normalize_lineage_component_rows(
            component_rows,
            resolve_component=lambda row: resolve_component_hash_for_registration(
                row=row,
                db_ctx=db_ctx,
                lineage_artifacts_by_id=lineage_artifacts_by_id,
                logger=logger,
            ),
            logger=logger,
        )
        candidate = build_lineage_composite_candidate(
            artifact=artifact,
            composite_digest=composite_digest,
            hashes=hashes,
            components=components,
            membership_index=membership_index,
            session_hash=session_hash,
            composite_builder=composite_builder,
            metadata=metadata,
            logger=logger,
        )
        if candidate is None:
            continue
        seen_hashes.add(composite_digest)
        payloads.append(candidate)

    return payloads


def resolve_component_hash_for_registration(
    *,
    row: dict[str, Any],
    db_ctx: Any,
    lineage_artifacts_by_id: dict[str, dict[str, Any]],
    logger: ILogger,
) -> tuple[str, str] | None:
    """Resolve a component digest for registration.

    Prefers a canonical blake3 (via a linked local artifact); otherwise falls back
    to the component's own digest under its declared algorithm. HF composite
    components carry sha256 (no blake3 exists), so this returns the sha256 digest
    rather than dropping the component.
    """
    artifact_id = row.get("artifact_id")
    linked_artifact: dict[str, Any] | None = None

    if isinstance(artifact_id, str) and artifact_id:
        linked_artifact = lineage_artifacts_by_id.get(artifact_id)
        if linked_artifact is None:
            artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
            if artifacts_repo is not None:
                loaded_artifact = artifacts_repo.get(artifact_id)
                if isinstance(loaded_artifact, dict):
                    linked_artifact = loaded_artifact
                    lineage_artifacts_by_id[artifact_id] = loaded_artifact

    if linked_artifact is not None:
        blake3_digest = select_hash_by_algorithm(linked_artifact, "blake3")
        if blake3_digest is None:
            blake3_digest = ensure_artifact_blake3_digest(
                db_ctx=db_ctx,
                artifact=linked_artifact,
                logger=logger,
            )
        if blake3_digest is not None:
            return "blake3", blake3_digest

    component_algorithm = row.get("component_algorithm")
    component_digest = row.get("component_digest")
    if (
        isinstance(component_algorithm, str)
        and isinstance(component_digest, str)
        and component_digest
    ):
        algo = component_algorithm.strip().lower()
        if algo in ("blake3", "sha256", "sha512", "md5"):
            return algo, component_digest.lower()

    return None


def extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize registration hashes with hash fallback enabled for lineage artifacts."""
    return normalize_registration_hashes(artifact, fallback_to_hash=True)
