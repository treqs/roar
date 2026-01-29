"""Base class for roar analyzers."""

from abc import ABC, abstractmethod


class Analyzer(ABC):
    """
    Base class for post-run analyzers.

    Analyzers examine tracer data and Python metadata to extract
    specific insights (experiment trackers, secrets, large files, etc.)
    """

    # Unique identifier for this analyzer
    name: str = "base"

    # Human-readable description
    description: str = "Base analyzer"

    def relevant(self, context: dict) -> bool:
        """
        Quick check if this analyzer should run.

        Override this to skip expensive analysis when clearly not needed.
        Default returns True (always run).

        Args:
            context: Dict with keys:
                - written_files: list of paths written during run
                - read_files: list of paths read during run
                - env: dict of environment variables
                - processes: list of process info dicts

        Returns:
            True if analyze() should be called
        """
        return True

    @abstractmethod
    def analyze(self, context: dict) -> dict | None:
        """
        Perform analysis and return findings.

        Args:
            context: Same as relevant(), plus:
                - repo_root: path to git repo root
                - tracer_data: raw tracer output
                - python_data: raw Python sitecustomize output (may be empty)

        Returns:
            Dict of findings to merge into provenance, or None if nothing found.
            The dict will be placed under provenance[self.name].
        """
        pass
