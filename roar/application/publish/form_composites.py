"""Shared composite-formation core for ``roar register`` and ``roar put``.

Given a set of ``(path, blake3)`` items — a run's recorded inputs/outputs, or a publish
source's files — group them into the dataset roots they belong to and build the
composite artifact for each. This is the single place the boundary commands agree on
*what a dataset is*, so register and put can never drift apart.

Policy (the crux):

* **auto** (``declared=False`` — the register path): a root is formed ONLY where a
  self-declaring manifest is present (Zarr ``.zgroup``/``.zarray``/``zarr.json``,
  LeRobot ``meta/info.json``, RLDS ``dataset_info.json``+``features.json``, Lance
  ``*.lance/``). Count-based shard heuristics (``>=2 .parquet`` / ``>=2 .tar``) are
  deliberately NOT a dataset here: a later run that read a *superset* of loose parquet
  would be a different "dataset", so the grouping has no stable identity. Very low
  false-positive rate is the whole point — we only claim structure we can recognize.

* **declared** (``declared=True`` — the put path): the publish act itself declares the
  boundary, so loose-shard (and even unstructured) groupings under a touched directory
  also composite. This is the only place the count heuristics are allowed.

Root inference is marker-anchored and works purely from the supplied paths/hashes — no
filesystem walk, no re-hashing — so it is O(files) and cheap to run over every job's
inputs and outputs at register time.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from .composite_builder import CompositeArtifactBuilder, CompositeBuildResult
from .source_resolution import ResolvedSource


def form_composites(
    items: list[tuple[str, str]],
    *,
    session_hash: str,
    declared: bool = False,
    source_type: str | None = None,
    builder: CompositeArtifactBuilder | None = None,
) -> list[CompositeBuildResult]:
    """Group ``(path, blake3)`` items into dataset roots and build their composites.

    Returns one :class:`CompositeBuildResult` per detected dataset (zero when nothing
    recognizable is present). See the module docstring for the auto/declared policy.
    """
    builder = builder or CompositeArtifactBuilder()
    hashes = dict(items)
    paths = list(hashes)

    roots = _outermost(r for p in paths if (r := _manifest_root(p)))
    if declared:
        roots = _outermost([*roots, *_declared_roots(roots, paths)])

    results: list[CompositeBuildResult] = []
    for root in roots:
        members = [p for p in paths if _under(p, root)]
        if len(members) < 2:
            continue
        resolved = [
            ResolvedSource(path=Path(p), exists=True, source_root=Path(root)) for p in members
        ]
        hashes_by_path = {str(Path(p)): hashes[p] for p in members}
        results.extend(
            builder.build_all_for_root(
                Path(root),
                resolved,
                hashes_by_path,
                session_hash,
                source_type,
                declared,
            )
        )
    return results


def _manifest_root(path: str) -> str | None:
    """The dataset root a self-declaring manifest file anchors, or ``None``.

    Anchors on the exact markers ``composite.detect`` keys off, so what register forms a
    root for is precisely what ``detect`` will classify as a dataset.
    """
    # A dataset rooted at the recording cwd (root == "") is intentionally not formed:
    # `_outermost` drops empty roots and `_under` can't host members relative to "". In
    # practice run-recorded paths are repo-relative with a containing dir, so this only
    # skips the degenerate "dataset is the whole working tree" case.
    base = posixpath.basename(path)
    if base in (".zgroup", ".zarray", "zarr.json"):
        return posixpath.dirname(path)
    if path == "meta/info.json" or path.endswith("/meta/info.json"):
        return path[: -len("meta/info.json")].rstrip("/") or None
    if base in ("dataset_info.json", "features.json"):
        return posixpath.dirname(path) or None
    norm = "/" + path
    if ".lance/" in norm:
        return norm[1 : norm.index(".lance/") + len(".lance")]
    if path.endswith(".lance"):
        return path
    return None


def _declared_roots(manifest_roots: list[str], paths: list[str]) -> list[str]:
    """Directories of loose files a *declared* publish should composite.

    Only reachable when ``declared=True``: groups files not already under a manifest
    root by their immediate parent directory; any parent with >=2 such files is a
    declared dataset root (``build_all_for_root(declared=True)`` then forms it whatever
    the shape — parquet shards, tar shards, or an unstructured pile).
    """
    by_parent: dict[str, int] = {}
    for p in paths:
        if any(_under(p, r) for r in manifest_roots):
            continue
        parent = posixpath.dirname(p)
        if parent:
            by_parent[parent] = by_parent.get(parent, 0) + 1
    return [parent for parent, count in by_parent.items() if count >= 2]


def _outermost(roots) -> list[str]:
    """Drop roots nested inside another root; keep the outermost of each branch."""
    unique = sorted({r for r in roots if r})
    kept: list[str] = []
    for r in unique:
        if not any(r == k or r.startswith(k + "/") for k in kept):
            kept.append(r)
    return kept


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")
