"""
Base telemetry provider.

Defines the interface for telemetry/experiment tracking providers.
"""

from abc import abstractmethod

from ...core.interfaces.telemetry import ITelemetryProvider, TelemetryRunInfo

# Re-export TelemetryRunInfo as TelemetryRun for convenience
TelemetryRun = TelemetryRunInfo


class BaseTelemetryProvider(ITelemetryProvider):
    """
    Abstract base class for telemetry providers.

    Implements the Strategy pattern for telemetry detection and extraction.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name (e.g., 'wandb', 'mlflow')."""
        pass

    @abstractmethod
    def detect_runs(
        self,
        repo_root: str,
        start_time: float,
        end_time: float,
        allow_incomplete: bool = False,
    ) -> list[TelemetryRun]:
        """
        Detect telemetry runs that were created during a job's execution.

        Args:
            repo_root: Path to the repository root
            start_time: Job start time (Unix timestamp)
            end_time: Job end time (Unix timestamp)
            allow_incomplete: If True, try to extract info from running jobs

        Returns:
            List of TelemetryRun objects found during the job
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
