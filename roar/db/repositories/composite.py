"""
SQLAlchemy composite artifact repository implementation.

Handles local persistence for composite artifact components and
membership index metadata.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Artifact, CompositeArtifactComponent, CompositeMembershipIndex


class SQLAlchemyCompositeRepository:
    """Repository for local composite artifact detail tables."""

    def __init__(self, session: Session):
        self._session = session

    def upsert_details(
        self,
        *,
        artifact_id: str,
        components: list[dict[str, Any]],
        component_count_total: int | None = None,
        membership_index: dict[str, Any] | None = None,
    ) -> None:
        artifact = self._session.get(Artifact, artifact_id)
        if artifact is None:
            raise ValueError(f"Composite artifact does not exist: {artifact_id}")

        self._update_artifact_metadata(artifact, component_count_total, len(components))
        self._upsert_components(artifact_id, components)
        self._upsert_membership_index(artifact_id, membership_index)
        self._session.flush()

    def _update_artifact_metadata(
        self,
        artifact: Artifact,
        component_count_total: int | None,
        component_count_fallback: int,
    ) -> None:
        artifact.kind = "composite"
        artifact.component_count = (
            component_count_total if component_count_total is not None else component_count_fallback
        )

    def _upsert_components(
        self,
        artifact_id: str,
        components: list[dict[str, Any]],
    ) -> None:
        self._session.execute(
            delete(CompositeArtifactComponent).where(
                CompositeArtifactComponent.composite_artifact_id == artifact_id
            )
        )
        for index, component in enumerate(components):
            self._session.add(
                CompositeArtifactComponent(
                    composite_artifact_id=artifact_id,
                    ordinal=index,
                    relative_path=str(component.get("relative_path") or ""),
                    leaf_kind=str(component.get("leaf_kind") or "file"),
                    component_algorithm=str(component.get("component_algorithm") or ""),
                    component_digest=str(component.get("component_digest") or "").lower(),
                    component_size=component.get("component_size"),
                    component_type=component.get("component_type"),
                    artifact_id=component.get("artifact_id"),
                )
            )

    def _upsert_membership_index(
        self,
        artifact_id: str,
        membership_index: dict[str, Any] | None,
    ) -> None:
        self._session.execute(
            delete(CompositeMembershipIndex).where(
                CompositeMembershipIndex.composite_artifact_id == artifact_id
            )
        )
        if membership_index is not None:
            self._session.add(
                CompositeMembershipIndex(
                    composite_artifact_id=artifact_id,
                    total_components=int(membership_index.get("total_components") or 0),
                    stored_components=int(membership_index.get("stored_components") or 0),
                    bloom_filter_base64=membership_index.get("bloom_filter_base64"),
                    bloom_bits=membership_index.get("bloom_bits"),
                    bloom_hashes=membership_index.get("bloom_hashes"),
                    bloom_version=membership_index.get("bloom_version"),
                )
            )

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self._session.get(Artifact, artifact_id)
        if artifact is None or artifact.kind != "composite":
            return None

        membership = self.get_membership_index(artifact_id)
        return {
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "component_count": artifact.component_count,
            "membership_index": membership,
        }

    def get_components(self, artifact_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        query = (
            select(CompositeArtifactComponent)
            .where(CompositeArtifactComponent.composite_artifact_id == artifact_id)
            .order_by(CompositeArtifactComponent.ordinal.asc(), CompositeArtifactComponent.id.asc())
            .limit(limit)
        )
        rows = self._session.execute(query).scalars().all()
        return [self._component_to_dict(row) for row in rows]

    def get_membership_index(self, artifact_id: str) -> dict[str, Any] | None:
        row = self._session.get(CompositeMembershipIndex, artifact_id)
        if row is None:
            return None
        return {
            "total_components": row.total_components,
            "stored_components": row.stored_components,
            "bloom_filter_base64": row.bloom_filter_base64,
            "bloom_bits": row.bloom_bits,
            "bloom_hashes": row.bloom_hashes,
            "bloom_version": row.bloom_version,
        }

    @staticmethod
    def _component_to_dict(component: CompositeArtifactComponent) -> dict[str, Any]:
        return {
            "id": component.id,
            "composite_artifact_id": component.composite_artifact_id,
            "ordinal": component.ordinal,
            "relative_path": component.relative_path,
            "leaf_kind": component.leaf_kind,
            "component_algorithm": component.component_algorithm,
            "component_digest": component.component_digest,
            "component_size": component.component_size,
            "component_type": component.component_type,
            "artifact_id": component.artifact_id,
        }
