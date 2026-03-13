"""Mechanical helpers for lineage composite preregistration."""

from __future__ import annotations

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
from ...glaas_client import GlaasClient
from .blake3_upgrade import ensure_artifact_blake3_digest, select_hash_by_algorithm


def has_lineage_composites(artifacts: list[dict[str, Any]]) -> bool:
    """Return whether any lineage artifact is a composite."""
    return any(extract_composite_digest(extract_registration_hashes(artifact)) for artifact in artifacts)


def preregister_lineage_composites_with_glaas(
    *,
    glaas_client: GlaasClient,
    db_ctx: Any,
    lineage_artifacts: list[dict[str, Any]],
    session_hash: str,
    registration_errors: list[str],
    composite_builder: Any,
    logger: ILogger,
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
    """Resolve a component digest to a canonical blake3 hash when possible."""
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
        and component_algorithm.strip().lower() == "blake3"
        and isinstance(component_digest, str)
        and component_digest
    ):
        return "blake3", component_digest.lower()

    return None


def extract_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize registration hashes with hash fallback enabled for lineage artifacts."""
    return normalize_registration_hashes(artifact, fallback_to_hash=True)
