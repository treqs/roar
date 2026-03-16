"""
Lineage collection contracts and payloads.

This module is the canonical home for publish-time lineage collection types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class LineageData:
    """Collected lineage data for publish and registration flows."""

    jobs: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    artifact_hashes: set[str] = field(default_factory=set)
    pipeline: dict | None = None


@runtime_checkable
class ILineageCollector(Protocol):
    """Protocol for collecting local lineage data."""

    def collect(
        self,
        artifact_hashes: list[str],
        roar_dir: Path,
    ) -> LineageData:
        """
        Collect lineage data for the given artifact hashes.

        Args:
            artifact_hashes: List of artifact hashes to trace.
            roar_dir: Path to `.roar`.

        Returns:
            LineageData with jobs and artifacts in the lineage.
        """
        ...
