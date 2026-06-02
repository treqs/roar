"""Dataset and composite-root planning helpers for publish workflows."""

from __future__ import annotations

import os
from pathlib import Path

from ...execution.recording import DatasetIdentifierInferer
from ..composite import detect
from .source_resolution import ResolvedSource


def infer_publish_dataset_identifiers(
    *,
    repo_root: Path,
    source_specs: list[str],
    resolved_sources: list[ResolvedSource],
    inferer: DatasetIdentifierInferer | None = None,
) -> list[dict[str, object]]:
    """Infer dataset identifiers from the publish source set."""
    active_inferer = inferer or DatasetIdentifierInferer()
    paths: list[str] = []

    for source in source_specs:
        if "://" in source:
            paths.append(source)
            continue

        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = repo_root / source_path
        paths.append(os.path.abspath(str(source_path)))

    for item in resolved_sources:
        paths.append(str(item.path))
        if item.source_root is not None:
            paths.append(str(item.source_root))

    unique_paths = [path for path in dict.fromkeys(paths) if path]
    return active_inferer.infer(unique_paths, repo_root=str(repo_root))


def detect_additional_publish_composite_roots(
    *,
    resolved_sources: list[ResolvedSource],
) -> dict[Path, list[ResolvedSource]]:
    """Group ungrouped publish files into composite roots by *structural* detection.

    Files supplied individually (no ``source_root``) are grouped by parent directory;
    a parent becomes a composite root only when its contents structurally declare a
    dataset (parquet shards, WebDataset, Zarr, …). Unstructured piles are not
    auto-composited. This replaces the prior confidence-scored heuristic and its
    "any two files under one parent" fallback — declared/structural, never scored.
    """
    ungrouped = [source for source in resolved_sources if source.source_root is None]
    if len(ungrouped) < 2:
        return {}

    by_parent: dict[Path, list[ResolvedSource]] = {}
    for source in ungrouped:
        by_parent.setdefault(source.path.parent, []).append(source)

    grouped: dict[Path, list[ResolvedSource]] = {}
    for parent, sources in by_parent.items():
        if len(sources) < 2:
            continue
        relpaths = [source.path.name for source in sources]
        if detect(relpaths).kind != "unstructured":
            grouped[parent] = sources

    return grouped
