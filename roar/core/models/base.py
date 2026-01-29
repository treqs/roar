"""
Base Pydantic models for roar.

Provides common configuration and base classes for all roar models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RoarBaseModel(BaseModel):
    """Base model for all roar Pydantic models.

    Configuration:
        - strict: Strict type coercion (no implicit conversions)
        - validate_assignment: Validate on attribute assignment
        - extra: Reject unknown fields
        - populate_by_name: Allow field aliases
        - use_enum_values: Serialize enums as values
        - revalidate_instances: Trust model instances (performance)
    """

    model_config = ConfigDict(
        strict=True,
        validate_assignment=True,
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
        revalidate_instances="never",
    )


class ImmutableModel(RoarBaseModel):
    """Immutable base model for DTOs that should not change after creation."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
        revalidate_instances="never",
    )
