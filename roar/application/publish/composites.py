"""Application-owned helpers for publish composite planning."""

from __future__ import annotations

from pathlib import Path

from .composite_builder import CompositeArtifactBuilder, CompositeBuildResult
from .source_resolution import ResolvedSource


def build_publish_composite_results(
    *,
    resolved_sources: list[ResolvedSource],
    hashes_by_path: dict[str, str],
    session_hash: str,
    source_type: str | None,
    additional_composite_roots: dict[Path, list[ResolvedSource]],
    composite_builder: CompositeArtifactBuilder,
) -> list[CompositeBuildResult]:
    """Build composite payloads for resolved publish roots."""
    grouped_by_root: dict[Path, list[ResolvedSource]] = {}

    for source in resolved_sources:
        if source.source_root is None:
            continue
        grouped_by_root.setdefault(source.source_root, []).append(source)

    for root_path, sources in additional_composite_roots.items():
        grouped_by_root.setdefault(root_path, list(sources))

    results: list[CompositeBuildResult] = []
    for root_path in sorted(grouped_by_root, key=lambda item: str(item)):
        result = composite_builder.build_for_root(
            root_path=root_path,
            resolved_sources=grouped_by_root[root_path],
            hashes_by_path=hashes_by_path,
            session_hash=session_hash,
            source_type=source_type,
        )
        if result is not None:
            results.append(result)

    return results
