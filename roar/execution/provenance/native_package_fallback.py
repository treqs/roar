"""Recover imported Python distributions from native file-tracer evidence.

The normal package collector is richer and remains authoritative.  This
fallback exists for Python invocations that suppress roar's ``sitecustomize``
injection (for example ``env PYTHONPATH=...``, ``python -E``, or ``python -I``).
Those invocations still leave exact package-file reads in the native trace.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
from collections import defaultdict
from collections.abc import Iterable

_PACKAGE_MARKERS = ("site-packages", "dist-packages")


def _package_root_and_top(path: str) -> tuple[str, str] | None:
    normalized = os.path.abspath(path)
    parts = normalized.split(os.sep)
    marker_index = next(
        (index for index, part in enumerate(parts) if part in _PACKAGE_MARKERS), None
    )
    if marker_index is None or marker_index + 1 >= len(parts):
        return None

    relative = parts[marker_index + 1 :]
    first = relative[0]
    if first.endswith((".dist-info", ".egg-info", ".pth")):
        return None
    if not (normalized.endswith((".py", ".pyc", ".so", ".pyd", ".dylib")) or ".so." in normalized):
        return None

    # Directory packages key on their first component.  A top-level module or
    # extension keys on the import name before its first suffix/ABI tag.
    top = first if len(relative) > 1 else first.split(".", 1)[0]
    root = os.sep.join(parts[: marker_index + 1]) or os.sep
    return root, top


def _distribution_top_names(dist: importlib_metadata.Distribution) -> set[str]:
    names: set[str] = set()
    declared = dist.read_text("top_level.txt")
    if declared:
        names.update(line.strip() for line in declared.splitlines() if line.strip())

    for file in dist.files or ():
        parts = str(file).replace("\\", "/").split("/")
        if not parts:
            continue
        first = parts[0]
        if first.endswith((".dist-info", ".egg-info", ".pth")):
            continue
        names.add(first if len(parts) > 1 else first.split(".", 1)[0])
    return names


def collect_native_python_packages(paths: Iterable[str]) -> dict[str, str | None]:
    """Map traced Python package-file reads to exact distribution pins."""
    requested: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        identified = _package_root_and_top(path)
        if identified:
            root, top = identified
            requested[root].add(top)

    packages: dict[str, str | None] = {}
    for root, imported_tops in requested.items():
        try:
            distributions = importlib_metadata.distributions(path=[root])
            for dist in distributions:
                if not imported_tops.intersection(_distribution_top_names(dist)):
                    continue
                name = dist.metadata["Name"]
                version = dist.version
                if name and version:
                    packages[name] = version
        except Exception:
            # Best effort: capture health remains marked degraded, so failure
            # here can never masquerade as a trustworthy empty environment.
            continue
    return dict(sorted(packages.items()))
