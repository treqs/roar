"""
Entity lookup service - resolves identifiers to their entities.

This service handles resolution of:
- Job UIDs
- Artifact hashes
- DAG/pipeline hashes
- Step references (@N, @BN)

Extracted from show.py to enable reuse across commands.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...db.context import DatabaseContext


class EntityType(Enum):
    """Type of resolved entity."""

    JOB = "job"
    ARTIFACT = "artifact"
    DAG = "dag"
    DAG_NODE = "dag_node"


@dataclass
class LookupResult:
    """Result of entity lookup."""

    entity_type: EntityType
    entity: dict[str, Any]
    source: str = "local"  # "local" or "glaas"


class EntityLookupService:
    """
    Resolves identifiers to their entities.

    Supports looking up:
    - Job UIDs (8 hex chars)
    - Artifact hashes (64 hex chars, prefix match supported)
    - DAG/pipeline hashes
    - Step references (@N for run steps, @BN for build steps)

    Usage:
        service = EntityLookupService(db_context)
        result = service.lookup("abc123de")
        if result:
            print(f"Found {result.entity_type}: {result.entity}")
    """

    def __init__(self, db_context: "DatabaseContext") -> None:
        """
        Initialize lookup service.

        Args:
            db_context: Database context for queries
        """
        self._db = db_context

    def lookup(self, identifier: str) -> LookupResult | None:
        """
        Resolve an identifier to its entity.

        Tries in order:
        1. Step reference (@N, @BN)
        2. Job UID
        3. Artifact hash
        4. DAG/pipeline hash (local, then GLaaS)

        Args:
            identifier: The identifier to resolve

        Returns:
            LookupResult if found, None otherwise
        """
        # Check for step reference first
        if identifier.startswith("@"):
            return self.lookup_dag_node(identifier)

        # Validate minimum length
        if len(identifier) < 4:
            return None

        # Try job UID (typically 8 hex chars)
        result = self.lookup_job(identifier)
        if result:
            return result

        # Try artifact hash (64 hex chars, prefix match)
        if len(identifier) >= 8:
            result = self.lookup_artifact(identifier)
            if result:
                return result

        # Try DAG/pipeline hash
        result = self.lookup_dag(identifier)
        if result:
            return result

        return None

    def lookup_job(self, uid: str) -> LookupResult | None:
        """
        Look up a job by UID.

        Args:
            uid: Job UID or prefix

        Returns:
            LookupResult if found, None otherwise
        """
        job = self._db.jobs.get_by_uid(uid)
        if job:
            return LookupResult(
                entity_type=EntityType.JOB,
                entity=job,
                source="local",
            )
        return None

    def lookup_artifact(self, hash_prefix: str) -> LookupResult | None:
        """
        Look up an artifact by hash prefix.

        Tries local database first, then GLaaS if configured.

        Args:
            hash_prefix: Artifact hash or prefix

        Returns:
            LookupResult if found, None otherwise
        """
        # Try local first
        artifact = self._db.artifacts.get_by_prefix(hash_prefix)
        if artifact:
            return LookupResult(
                entity_type=EntityType.ARTIFACT,
                entity=artifact,
                source="local",
            )

        # Try GLaaS
        artifact = self._lookup_artifact_glaas(hash_prefix)
        if artifact:
            return LookupResult(
                entity_type=EntityType.ARTIFACT,
                entity=artifact,
                source="glaas",
            )

        return None

    def lookup_dag(self, hash_prefix: str) -> LookupResult | None:
        """
        Look up a DAG/pipeline by hash.

        Tries local database first, then GLaaS if configured.

        Args:
            hash_prefix: DAG hash or prefix

        Returns:
            LookupResult if found, None otherwise
        """
        from sqlalchemy import text

        # Try exact match first
        pipeline = self._db.sessions.get_by_hash(hash_prefix)

        # Try prefix match if no exact match
        if not pipeline:
            cursor = self._db.conn.execute(
                text("SELECT * FROM sessions WHERE hash LIKE :hash_prefix LIMIT 1"),
                {"hash_prefix": f"{hash_prefix}%"},
            )
            row = cursor.fetchone()
            if row:
                pipeline = dict(row)

        if pipeline:
            return LookupResult(
                entity_type=EntityType.DAG,
                entity=pipeline,
                source="local",
            )

        return None

    def lookup_dag_node(self, reference: str) -> LookupResult | None:
        """
        Look up a DAG node by step reference.

        Args:
            reference: Step reference (@N or @BN)

        Returns:
            LookupResult if found, None otherwise
        """
        from .step_parser import StepReferenceError, parse_step_reference

        try:
            ref = parse_step_reference(reference)
        except StepReferenceError:
            return None

        # Get active pipeline
        pipeline = self._db.sessions.get_active()
        if not pipeline:
            return None

        # Get the step
        job_type = "build" if ref.is_build else None
        step = self._db.sessions.get_step_by_number(
            pipeline["id"], ref.step_number, job_type=job_type
        )

        if not step:
            return None

        return LookupResult(
            entity_type=EntityType.DAG_NODE,
            entity=step,
            source="local",
        )

    def _lookup_artifact_glaas(self, hash_prefix: str) -> dict | None:
        """Look up artifact on GLaaS."""
        try:
            from ...glaas_client import GlaasClient

            glaas = GlaasClient()
            if not glaas.is_configured():
                return None

            artifact, _error = glaas.get_artifact(hash_prefix)
            return artifact
        except Exception:
            return None


def resolve_identifier(db_context: "DatabaseContext", identifier: str) -> LookupResult | None:
    """
    Convenience function to resolve an identifier.

    Args:
        db_context: Database context
        identifier: The identifier to resolve

    Returns:
        LookupResult if found, None otherwise
    """
    service = EntityLookupService(db_context)
    return service.lookup(identifier)
