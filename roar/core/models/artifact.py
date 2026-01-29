"""
Artifact domain models.

Provides Pydantic models for content-addressed artifacts and their hashes.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator

from .base import ImmutableModel, RoarBaseModel

# Type aliases for validation
HashAlgorithm = Literal["blake3", "sha256", "sha512", "md5"]
HexDigest = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[a-f0-9]+$")]


class ArtifactHash(ImmutableModel):
    """Single hash for an artifact."""

    algorithm: HashAlgorithm
    digest: HexDigest

    @field_validator("digest", mode="before")
    @classmethod
    def normalize_digest(cls, v: str) -> str:
        """Normalize digest to lowercase."""
        if isinstance(v, str):
            return v.lower()
        return v


class Artifact(RoarBaseModel):
    """Represents a tracked artifact with its hashes and metadata.

    Artifacts are content-addressed files tracked by their hash digests.
    Each artifact can have multiple hashes (blake3, sha256, etc.).
    """

    id: Annotated[str, Field(min_length=1, max_length=64)]
    size: Annotated[int, Field(ge=0, description="File size in bytes")]
    first_seen_at: Annotated[float, Field(gt=0, description="Unix timestamp")]
    first_seen_path: str | None = None
    source_type: Annotated[str, Field(max_length=32)] | None = None
    source_url: Annotated[str, Field(max_length=2048)] | None = None
    uploaded_to: str | None = None
    synced_at: Annotated[float, Field(gt=0)] | None = None
    metadata: str | None = None
    hashes: list[ArtifactHash] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def primary_hash(self) -> str | None:
        """Get the primary hash digest (blake3 if available, else first)."""
        for h in self.hashes:
            if h.algorithm == "blake3":
                return h.digest
        return self.hashes[0].digest if self.hashes else None

    @classmethod
    def from_orm(
        cls,
        orm_artifact: object,
        hashes: list[dict[str, str]] | None = None,
    ) -> Artifact:
        """Create Artifact from ORM model.

        Args:
            orm_artifact: SQLAlchemy Artifact model instance
            hashes: List of hash dicts [{"algorithm": "blake3", "digest": "..."}]

        Returns:
            Artifact pydantic model instance
        """
        hash_models = []
        if hashes:
            for h in hashes:
                hash_models.append(
                    ArtifactHash(algorithm=h["algorithm"], digest=h["digest"])  # type: ignore[arg-type]
                )

        return cls(
            id=orm_artifact.id,  # type: ignore[attr-defined]
            size=orm_artifact.size,  # type: ignore[attr-defined]
            first_seen_at=orm_artifact.first_seen_at,  # type: ignore[attr-defined]
            first_seen_path=orm_artifact.first_seen_path,  # type: ignore[attr-defined]
            source_type=orm_artifact.source_type,  # type: ignore[attr-defined]
            source_url=orm_artifact.source_url,  # type: ignore[attr-defined]
            uploaded_to=orm_artifact.uploaded_to,  # type: ignore[attr-defined]
            synced_at=orm_artifact.synced_at,  # type: ignore[attr-defined]
            metadata=getattr(orm_artifact, "metadata_", None),
            hashes=hash_models,
        )
