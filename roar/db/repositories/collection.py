"""
SQLAlchemy collection repository implementation.

Handles collection management for grouping artifacts.
"""

import time
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...core.interfaces.repositories import CollectionRepository
from ..models import Artifact, Collection, CollectionMember


class SQLAlchemyCollectionRepository(CollectionRepository):
    """
    SQLAlchemy implementation of collection repository.

    Manages collections which group artifacts and can be nested.
    """

    def __init__(self, session: Session):
        """
        Initialize repository with database session.

        Args:
            session: SQLAlchemy session
        """
        self._session = session

    def create(
        self,
        name: str,
        collection_type: str | None = None,
        source_type: str | None = None,
        source_url: str | None = None,
        metadata: str | None = None,
    ) -> int:
        """
        Create a new collection.

        Args:
            name: Collection name
            collection_type: Type of collection
            source_type: Source type
            source_url: Source URL
            metadata: JSON metadata string

        Returns:
            Collection ID.
        """
        collection = Collection(
            name=name,
            collection_type=collection_type,
            source_type=source_type,
            source_url=source_url,
            created_at=time.time(),
            metadata_=metadata,
        )
        self._session.add(collection)
        self._session.flush()
        return collection.id

    def add_artifact(
        self,
        collection_id: int,
        artifact_id: str,
        path_in_collection: str | None = None,
    ) -> None:
        """
        Add an artifact to a collection.

        Args:
            collection_id: Collection ID
            artifact_id: Artifact UUID
            path_in_collection: Path within the collection
        """
        # Check if already exists
        existing = self._session.execute(
            select(CollectionMember).where(
                CollectionMember.collection_id == collection_id,
                CollectionMember.artifact_id == artifact_id,
            )
        ).scalar_one_or_none()

        if not existing:
            member = CollectionMember(
                collection_id=collection_id,
                artifact_id=artifact_id,
                path_in_collection=path_in_collection,
            )
            self._session.add(member)
            self._session.flush()

    def add_child(
        self,
        parent_id: int,
        child_id: int,
        path_in_collection: str | None = None,
    ) -> None:
        """
        Add a child collection to a parent collection.

        Args:
            parent_id: Parent collection ID
            child_id: Child collection ID
            path_in_collection: Path within the parent collection
        """
        # Check if already exists
        existing = self._session.execute(
            select(CollectionMember).where(
                CollectionMember.collection_id == parent_id,
                CollectionMember.child_collection_id == child_id,
            )
        ).scalar_one_or_none()

        if not existing:
            member = CollectionMember(
                collection_id=parent_id,
                child_collection_id=child_id,
                path_in_collection=path_in_collection,
            )
            self._session.add(member)
            self._session.flush()

    def update_upload(self, collection_id: int, uploaded_to: str) -> None:
        """
        Record that a collection was uploaded to a destination.

        Args:
            collection_id: Collection ID
            uploaded_to: Upload destination URL
        """
        self._session.execute(
            update(Collection).where(Collection.id == collection_id).values(uploaded_to=uploaded_to)
        )
        self._session.flush()

    def get(self, collection_id: int) -> dict[str, Any] | None:
        """
        Get a collection by ID.

        Args:
            collection_id: Collection ID

        Returns:
            Collection dict or None if not found.
        """
        collection = self._session.get(Collection, collection_id)
        return self._collection_to_dict(collection) if collection else None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        """
        Get a collection by name.

        Args:
            name: Collection name

        Returns:
            Collection dict or None if not found.
        """
        collection = self._session.execute(
            select(Collection).where(Collection.name == name)
        ).scalar_one_or_none()
        return self._collection_to_dict(collection) if collection else None

    def get_by_source(self, source_url: str) -> dict[str, Any] | None:
        """
        Get a collection by its source URL.

        Args:
            source_url: Source URL

        Returns:
            Collection dict or None if not found.
        """
        collection = self._session.execute(
            select(Collection).where(Collection.source_url == source_url)
        ).scalar_one_or_none()
        return self._collection_to_dict(collection) if collection else None

    def get_members(self, collection_id: int, artifact_repo) -> dict[str, list[dict[str, Any]]]:
        """
        Get all members of a collection (artifacts and child collections).

        Args:
            collection_id: Collection ID
            artifact_repo: Artifact repository for fetching hashes

        Returns:
            Dict with 'artifacts' and 'children' lists.
        """
        # Get artifact members
        artifact_query = (
            select(CollectionMember.path_in_collection, Artifact)
            .join(Artifact, CollectionMember.artifact_id == Artifact.id)
            .where(
                CollectionMember.collection_id == collection_id,
                CollectionMember.artifact_id.isnot(None),
            )
        )
        artifact_rows = self._session.execute(artifact_query).all()

        artifacts = []
        for path_in_collection, artifact in artifact_rows:
            artifact_dict = self._artifact_to_dict(artifact)
            artifact_dict["path_in_collection"] = path_in_collection
            artifact_dict["hashes"] = artifact_repo.get_hashes(artifact.id)
            artifacts.append(artifact_dict)

        # Get child collection members
        child_query = (
            select(CollectionMember.path_in_collection, Collection)
            .join(Collection, CollectionMember.child_collection_id == Collection.id)
            .where(
                CollectionMember.collection_id == collection_id,
                CollectionMember.child_collection_id.isnot(None),
            )
        )
        child_rows = self._session.execute(child_query).all()

        children = []
        for path_in_collection, child in child_rows:
            child_dict = self._collection_to_dict(child)
            child_dict["path_in_collection"] = path_in_collection
            children.append(child_dict)

        return {"artifacts": artifacts, "children": children}

    def get_all_artifacts(self, collection_id: int, artifact_repo) -> list[dict[str, Any]]:
        """
        Get all artifacts in a collection (flattened, including nested).

        Args:
            collection_id: Collection ID
            artifact_repo: Artifact repository for fetching hashes

        Returns:
            List of all artifacts, including from nested collections.
        """
        result = []
        visited = set()

        def collect(coll_id: int):
            if coll_id in visited:
                return
            visited.add(coll_id)

            members = self.get_members(coll_id, artifact_repo)
            result.extend(members["artifacts"])
            for child in members["children"]:
                collect(child["id"])

        collect(collection_id)
        return result

    def _collection_to_dict(self, collection: Collection) -> dict[str, Any]:
        """Convert Collection model to dict."""
        return {
            "id": collection.id,
            "name": collection.name,
            "collection_type": collection.collection_type,
            "source_type": collection.source_type,
            "source_url": collection.source_url,
            "uploaded_to": collection.uploaded_to,
            "created_at": collection.created_at,
            "synced_at": collection.synced_at,
            "metadata": collection.metadata_,
        }

    def _artifact_to_dict(self, artifact: Artifact) -> dict[str, Any]:
        """Convert Artifact model to dict."""
        return {
            "id": artifact.id,
            "size": artifact.size,
            "first_seen_at": artifact.first_seen_at,
            "first_seen_path": artifact.first_seen_path,
            "source_type": artifact.source_type,
            "source_url": artifact.source_url,
            "uploaded_to": artifact.uploaded_to,
            "synced_at": artifact.synced_at,
            "metadata": artifact.metadata_,
        }


# Backward compatibility alias
SQLiteCollectionRepository = SQLAlchemyCollectionRepository
