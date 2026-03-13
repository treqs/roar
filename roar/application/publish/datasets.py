"""Dataset and composite-root planning helpers for publish workflows."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from ...services.execution.dataset_identifier import DatasetIdentifierInferer
from .source_resolution import ResolvedSource

_AUTO_COMPOSITE_MIN_CONFIDENCE = 0.80


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
    dataset_identifiers: list[dict[str, object]],
) -> dict[Path, list[ResolvedSource]]:
    """Detect composite roots implied by dataset identifiers and grouped files."""
    ungrouped = [source for source in resolved_sources if source.source_root is None]
    if len(ungrouped) < 2:
        return {}

    grouped: dict[Path, list[ResolvedSource]] = {}
    assigned_paths: set[str] = set()

    for candidate in dataset_identifiers:
        confidence = candidate.get("confidence")
        if not isinstance(confidence, (int, float)):
            continue
        if float(confidence) < _AUTO_COMPOSITE_MIN_CONFIDENCE:
            continue

        dataset_id = str(candidate.get("dataset_id", ""))
        parsed = urlparse(dataset_id)
        if parsed.scheme != "file" or not parsed.path:
            continue

        root = Path(parsed.path)
        matches = [
            source
            for source in ungrouped
            if str(source.path) not in assigned_paths and source.path.is_relative_to(root)
        ]
        if len(matches) < 2:
            continue

        grouped[root] = matches
        assigned_paths.update(str(source.path) for source in matches)

    fallback_by_parent: dict[Path, list[ResolvedSource]] = {}
    for source in ungrouped:
        if str(source.path) in assigned_paths:
            continue
        fallback_by_parent.setdefault(source.path.parent, []).append(source)

    for parent, sources in fallback_by_parent.items():
        if len(sources) >= 2:
            grouped[parent] = sources

    return grouped
