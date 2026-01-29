"""
Presenter interface definitions for output formatting.

Enables pluggable output formats (console, JSON, etc.)
following the Interface Segregation Principle.
"""

from abc import ABC, abstractmethod
from typing import Any


class IPresenter(ABC):
    """
    Interface for output presentation.

    Implementations handle formatting and displaying output
    to the user in various formats (console, JSON, etc.).
    """

    @abstractmethod
    def print(self, message: str) -> None:
        """Print a message to output."""
        pass

    @abstractmethod
    def print_error(self, message: str) -> None:
        """Print an error message."""
        pass

    @abstractmethod
    def print_table(self, headers: list[str], rows: list[list[str]]) -> None:
        """
        Print a table.

        Args:
            headers: Column headers
            rows: Table rows (list of row values)
        """
        pass

    @abstractmethod
    def print_job(self, job: dict[str, Any], verbose: bool = False) -> None:
        """
        Print job details.

        Args:
            job: Job dictionary
            verbose: Whether to show extended details
        """
        pass

    @abstractmethod
    def print_artifact(self, artifact: dict[str, Any]) -> None:
        """
        Print artifact details.

        Args:
            artifact: Artifact dictionary
        """
        pass

    @abstractmethod
    def print_dag(
        self,
        summary: dict[str, Any],
        stale_steps: set[int] | None = None,
    ) -> None:
        """
        Print DAG summary.

        Args:
            summary: Pipeline summary dictionary
            stale_steps: Set of stale step numbers
        """
        pass

    @abstractmethod
    def confirm(self, message: str, default: bool = False) -> bool:
        """
        Ask for user confirmation.

        Args:
            message: Confirmation prompt
            default: Default value if user presses Enter

        Returns:
            True if confirmed, False otherwise
        """
        pass
