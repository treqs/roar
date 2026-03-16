"""Mechanical helpers for put composite registration and persistence."""

from __future__ import annotations

from typing import Any, cast

from ...application.publish.composite_builder import CompositeBuildResult
from ...application.publish.metadata import (
    build_local_publish_composite_metadata_json,
    build_publish_composite_dataset_metadata_json,
)
from ...application.publish.registration import (
    CompositeRegistrationCandidate,
    build_lineage_composite_candidate,
    extract_composite_digest,
    normalize_lineage_component_rows,
    normalize_registration_hashes,
    parse_composite_registration_response,
    preregister_lineage_composites,
    resolve_lineage_component_count_total,
)
from ...core.interfaces.logger import ILogger
from ...db.context import optional_repo
from ...integrations.glaas import GlaasClient
from ...integrations.glaas.registration import _artifact_ref


def preregister_put_lineage_composites_with_glaas(
    *,
    db_ctx: Any,
    glaas_client: GlaasClient,
    lineage_artifacts: list[dict[str, Any]],
    session_hash: str,
    registration_errors: list[str],
    dataset_identifiers: list[dict[str, Any]] | None,
    composite_builder: Any,
    logger: ILogger,
) -> list[dict[str, Any]]:
    """Prepare and preregister lineage composites for the put workflow."""
    payloads = build_put_lineage_composite_payloads(
        db_ctx=db_ctx,
        lineage_artifacts=lineage_artifacts,
        session_hash=session_hash,
        dataset_identifiers=dataset_identifiers,
        composite_builder=composite_builder,
        logger=logger,
    )
    return preregister_lineage_composites(
        glaas_client=glaas_client,
        payloads=payloads,
        registration_errors=registration_errors,
        logger=logger,
    )


def build_put_lineage_composite_payloads(
    *,
    db_ctx: Any,
    lineage_artifacts: list[dict[str, Any]],
    session_hash: str,
    dataset_identifiers: list[dict[str, Any]] | None,
    composite_builder: Any,
    logger: ILogger,
) -> list[CompositeRegistrationCandidate]:
    """Build local lineage composite registration payloads for put."""
    composites_repo = cast(Any, optional_repo(db_ctx, "composites"))
    payloads: list[CompositeRegistrationCandidate] = []
    seen_hashes: set[str] = set()

    for artifact in lineage_artifacts:
        hashes = extract_put_registration_hashes(artifact)
        composite_digest = extract_composite_digest(hashes)
        if not composite_digest or composite_digest in seen_hashes:
            continue

        component_rows: list[dict[str, Any]] = []
        membership_index: dict[str, Any] | None = None
        artifact_id = artifact.get("id")
        if composites_repo is not None and isinstance(artifact_id, str) and artifact_id:
            try:
                rows = composites_repo.get_components(artifact_id, limit=5000)
                if isinstance(rows, list):
                    component_rows = [row for row in rows if isinstance(row, dict)]
            except Exception as exc:  # pragma: no cover - defensive path
                logger.warning(
                    "Failed to load local components for lineage composite %s: %s",
                    composite_digest[:12],
                    exc,
                )
            try:
                raw_membership = composites_repo.get_membership_index(artifact_id)
                if isinstance(raw_membership, dict):
                    membership_index = raw_membership
            except Exception as exc:  # pragma: no cover - defensive path
                logger.warning(
                    "Failed to load local membership index for lineage composite %s: %s",
                    composite_digest[:12],
                    exc,
                )

        components = normalize_lineage_component_rows(
            component_rows,
            resolve_component=resolve_put_lineage_component_for_registration,
            logger=logger,
        )

        seen_hashes.add(composite_digest)
        metadata_json = build_publish_composite_dataset_metadata_json(
            root_path=str(_artifact_ref.artifact_path(artifact) or ""),
            dataset_identifiers=dataset_identifiers,
            artifact_metadata=artifact.get("metadata"),
            components=components,
            component_count_total=resolve_lineage_component_count_total(
                artifact_component_count=artifact.get("component_count"),
                membership_index=membership_index,
                stored_components=len(components),
            ),
        )
        candidate = build_lineage_composite_candidate(
            artifact=artifact,
            composite_digest=composite_digest,
            hashes=hashes,
            components=components,
            membership_index=membership_index,
            session_hash=session_hash,
            composite_builder=composite_builder,
            metadata=metadata_json,
            logger=logger,
        )
        if candidate is None:
            continue
        payloads.append(candidate)

    return payloads


def resolve_put_lineage_component_for_registration(
    row: dict[str, Any],
) -> tuple[str, str] | None:
    """Resolve stored lineage component rows for put composite registration."""
    component_algorithm = row.get("component_algorithm")
    component_digest = row.get("component_digest")
    if (
        isinstance(component_algorithm, str)
        and component_algorithm.strip()
        and isinstance(component_digest, str)
        and component_digest
    ):
        return component_algorithm.strip().lower(), component_digest.lower()
    return None


def register_put_composites_with_glaas(
    *,
    db_ctx: Any,
    glaas_client: GlaasClient,
    composite_results: list[CompositeBuildResult],
    registration_errors: list[str],
    dataset_identifiers: list[dict[str, Any]] | None,
    logger: ILogger,
) -> list[dict[str, Any]]:
    """Register generated composite artifacts with GLaaS and persist local state."""
    composite_registrations: list[dict[str, Any]] = []

    for composite in composite_results:
        payload = dict(composite.payload)
        metadata_json = build_publish_composite_dataset_metadata_json(
            root_path=composite.root_path,
            dataset_identifiers=dataset_identifiers,
            components=list(composite.payload.get("components") or []),
            component_count_total=composite.component_count_total,
        )
        if metadata_json is not None:
            payload["metadata"] = metadata_json

        response = glaas_client.register_composite_artifact(payload)
        result, error = parse_composite_registration_response(response)

        composite_registration: dict[str, Any] = {
            "root_path": composite.root_path,
            "hash": composite.digest,
            "component_count_total": composite.component_count_total,
            "component_count_stored": composite.component_count_stored,
        }
        if error:
            composite_registration["registered"] = False
            composite_registration["error"] = error
            registration_errors.append(f"Composite {composite.digest[:12]}: {error}")
        else:
            composite_registration["registered"] = True
            if isinstance(result, dict):
                if "artifact_id" in result:
                    composite_registration["artifact_id"] = result["artifact_id"]
                if "created" in result:
                    composite_registration["created"] = result["created"]
            local_persist_error = persist_local_put_composite_registration(
                db_ctx=db_ctx,
                composite=composite,
                composite_registration=composite_registration,
                dataset_identifiers=dataset_identifiers,
                logger=logger,
            )
            if local_persist_error:
                composite_registration["local_persisted"] = False
                composite_registration["local_error"] = local_persist_error
            else:
                composite_registration["local_persisted"] = True
        composite_registrations.append(composite_registration)

    return composite_registrations


def persist_local_put_composite_registration(
    *,
    db_ctx: Any,
    composite: CompositeBuildResult,
    composite_registration: dict[str, Any],
    dataset_identifiers: list[dict[str, Any]] | None,
    logger: ILogger,
) -> str | None:
    """Persist a successfully registered composite into roar.db."""
    artifacts_repo = cast(Any, optional_repo(db_ctx, "artifacts"))
    composites_repo = cast(Any, optional_repo(db_ctx, "composites"))
    if artifacts_repo is None or composites_repo is None:
        return None

    try:
        metadata = build_local_publish_composite_metadata_json(
            composite=composite,
            dataset_identifiers=dataset_identifiers,
        )
        local_artifact_id, _created = artifacts_repo.register(
            hashes={"composite-blake3": composite.digest},
            size=int(composite.payload.get("size") or 0),
            path=composite.root_path,
            source_type=composite.payload.get("source_type"),
            metadata=metadata,
        )
        composites_repo.upsert_details(
            artifact_id=local_artifact_id,
            components=list(composite.payload.get("components") or []),
            component_count_total=composite.component_count_total,
            membership_index=composite.payload.get("membership_index"),
        )
        composite_registration["local_artifact_id"] = local_artifact_id
        return None
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Local composite persistence failed for %s: %s",
            composite.digest[:12],
            exc,
        )
        return str(exc)


def extract_put_registration_hashes(artifact: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize registration hashes for put composite preregistration."""
    return normalize_registration_hashes(artifact)
