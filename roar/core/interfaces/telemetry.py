"""
Telemetry provider interface definitions.

Enables pluggable experiment tracking providers (W&B, MLflow, Neptune, etc.)
following the Open/Closed Principle.
"""

from abc import ABC, abstractmethod

# Re-export models for backward compatibility
from roar.core.models.telemetry import TelemetryRunInfo


class ITelemetryProvider(ABC):
    """
    Interface for telemetry/experiment tracking providers.

    Implementations detect and extract run information from
    experiment tracking tools used during job execution.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Provider identifier.

        Examples: 'wandb', 'mlflow', 'neptune', 'tensorboard'
        """
        pass

    @abstractmethod
    def detect_runs(
        self,
        repo_root: str,
        start_time: float,
        end_time: float,
        allow_incomplete: bool = False,
    ) -> list[TelemetryRunInfo]:
        """
        Detect runs created during the specified time window.

        Args:
            repo_root: Directory to search (typically repo root)
            start_time: Job start time (Unix timestamp)
            end_time: Job end time (Unix timestamp)
            allow_incomplete: If True, try to extract from running jobs

        Returns:
            List of detected run info objects
        """
        pass

    @abstractmethod
    def get_run_url(self, run_id: str) -> str | None:
        """
        Get the URL for a specific run.

        Args:
            run_id: The run identifier

        Returns:
            URL to the run, or None if not found
        """
        pass

    def is_available(self) -> bool:
        """
        Check if this telemetry provider is available.

        Returns:
            True if the required library is installed
        """
        return True
