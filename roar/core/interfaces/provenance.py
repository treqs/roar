"""
Protocol definitions for provenance services.

This module defines the interfaces and data structures used by the provenance
collection system following the dependency inversion principle.
"""

from typing import Any, Protocol, runtime_checkable

# Re-export models for backward compatibility
from roar.core.models.provenance import (
    FilteredFiles,
    ProvenanceContext,
    PythonInjectData,
    RuntimeInfo,
    TracerData,
)


@runtime_checkable
class IDataLoader(Protocol):
    """Protocol for loading tracer and Python inject data."""

    def load_tracer_data(self, path: str) -> TracerData:
        """Load tracer JSON output."""
        ...

    def load_python_data(self, path: str | None) -> PythonInjectData:
        """Load Python inject JSON output (optional)."""
        ...


@runtime_checkable
class IFileFilterService(Protocol):
    """Protocol for filtering files based on configuration."""

    def filter_files(
        self,
        tracer_data: TracerData,
        python_data: PythonInjectData,
        config: dict[str, Any],
    ) -> FilteredFiles:
        """Apply filters to file lists."""
        ...


@runtime_checkable
class IRuntimeCollector(Protocol):
    """Protocol for collecting runtime environment information."""

    def collect(
        self,
        python_data: PythonInjectData,
        tracer_data: TracerData,
        timing: dict[str, Any],
    ) -> RuntimeInfo:
        """Collect runtime environment info."""
        ...


@runtime_checkable
class IProcessSummarizer(Protocol):
    """Protocol for summarizing process trees."""

    def summarize(self, processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Summarize process tree by collapsing fork-only duplicates."""
        ...


@runtime_checkable
class IPackageCollector(Protocol):
    """Protocol for collecting package information."""

    def collect(
        self,
        python_data: PythonInjectData,
        shared_libs: list[str],
        sys_prefix: str,
    ) -> dict[str, dict[str, str]]:
        """Collect package info organized by manager (pip, dpkg, etc.)."""
        ...


@runtime_checkable
class IProvenanceAssembler(Protocol):
    """Protocol for assembling final provenance output."""

    def assemble(self, ctx: ProvenanceContext, config: dict[str, Any]) -> dict[str, Any]:
        """Assemble final provenance dict from context."""
        ...
