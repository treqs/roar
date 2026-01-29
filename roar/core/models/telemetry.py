"""
Telemetry domain models.

Provides Pydantic models for experiment tracking provider information.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, computed_field

from .base import ImmutableModel

# Known telemetry providers
TelemetryProvider = Literal["wandb", "mlflow", "neptune", "tensorboard", "comet", "aim"]


class TelemetryRunInfo(ImmutableModel):
    """Information about a detected telemetry/experiment tracking run.

    Contains details about experiment tracking runs detected during
    command execution.
    """

    provider: TelemetryProvider
    run_id: str | None = None
    project: str | None = None
    entity: str | None = None
    url: Annotated[str, Field(max_length=2048)] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """Get a human-readable display name for the run."""
        if self.project and self.run_id:
            return f"{self.provider}:{self.project}/{self.run_id}"
        return f"{self.provider}:{self.run_id or 'unknown'}"
