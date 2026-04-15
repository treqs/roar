"""
Local label repository implementation.

Stores versioned label documents against sessions, jobs, and artifacts.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ...core.interfaces.repositories import LabelRepository
from ..models import Label


class SQLAlchemyLabelRepository(LabelRepository):
    """SQLAlchemy implementation of the local label repository."""

    def __init__(self, session: Session):
        self._session = session

    def get_current(
        self,
        entity_type: str,
        *,
        session_id: int | None = None,
        job_id: int | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any] | None:
        row = self._session.execute(
            select(Label)
            .where(self._target_clause(entity_type, session_id, job_id, artifact_id))
            .order_by(Label.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return self._label_to_dict(row) if row else None

    def get_history(
        self,
        entity_type: str,
        *,
        session_id: int | None = None,
        job_id: int | None = None,
        artifact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = (
            self._session.execute(
                select(Label)
                .where(self._target_clause(entity_type, session_id, job_id, artifact_id))
                .order_by(Label.version.asc())
            )
            .scalars()
            .all()
        )
        return [self._label_to_dict(row) for row in rows]

    def create_version(
        self,
        entity_type: str,
        metadata: dict[str, Any],
        *,
        session_id: int | None = None,
        job_id: int | None = None,
        artifact_id: str | None = None,
        write_origin: str | None = None,
    ) -> dict[str, Any]:
        current_max = self._session.execute(
            select(func.max(Label.version)).where(
                self._target_clause(entity_type, session_id, job_id, artifact_id)
            )
        ).scalar_one()
        version = int(current_max or 0) + 1

        row = Label(
            entity_type=entity_type,
            session_id=session_id,
            job_id=job_id,
            artifact_id=artifact_id,
            version=version,
            metadata_=json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            write_origin=write_origin,
            created_at=time.time(),
        )
        self._session.add(row)
        self._session.flush()
        return self._label_to_dict(row)

    @staticmethod
    def _target_clause(
        entity_type: str,
        session_id: int | None,
        job_id: int | None,
        artifact_id: str | None,
    ):
        return and_(
            Label.entity_type == entity_type,
            Label.session_id.is_(session_id)
            if session_id is None
            else Label.session_id == session_id,
            Label.job_id.is_(job_id) if job_id is None else Label.job_id == job_id,
            Label.artifact_id.is_(artifact_id)
            if artifact_id is None
            else Label.artifact_id == artifact_id,
        )

    @staticmethod
    def _label_to_dict(row: Label) -> dict[str, Any]:
        return {
            "id": row.id,
            "entity_type": row.entity_type,
            "session_id": row.session_id,
            "job_id": row.job_id,
            "artifact_id": row.artifact_id,
            "version": row.version,
            "metadata": json.loads(row.metadata_),
            "write_origin": row.write_origin,
            "created_at": row.created_at,
            "synced_at": row.synced_at,
            "synced_server_label_id": row.synced_server_label_id,
        }


SQLiteLabelRepository = SQLAlchemyLabelRepository
