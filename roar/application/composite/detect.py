"""Structural dataset detection — declared/structural, never scored.

Each detector is a pure function of a file listing (relpaths) and returns a
:class:`Detection` naming the structural ``kind``, which files are identity-bearing
(``data``/``meta``) versus excluded ``boilerplate``, and any nested sub-datasets.

This replaces the role of the confidence scorer: a format's on-disk structure
either declares it or it does not. There are no thresholds or weights. When nothing
matches, ``kind`` is ``"unstructured"`` and the caller decides (an explicit
``roar put --dataset`` may still declare it; an unstructured pile falls to the
download size-gate when imported from a host).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

# Repo/git boilerplate present everywhere — never data, never identity-bearing.
#
# IDENTITY CONTRACT: this set is excluded from every composite digest (both the get
# sha256-tree and the put/register blake3-tree paths derive their exclusions from
# `detect().boilerplate`). It is therefore part of the `roar:{combiner}-tree:v1` digest
# domain: changing a member silently re-digests existing datasets and breaks the
# put-export <-> get-import identity guarantee. Any edit here MUST bump the tree domain
# version (v1 -> v2) in `canonical.py` and `composite_builder.py`.
BOILERPLATE = frozenset({".gitattributes", ".gitignore", "README.md", "LICENSE", ".DS_Store"})


@dataclass(frozen=True)
class Detection:
    kind: str
    data: list[str]  # identity-bearing data files
    meta: list[str]  # identity-bearing, format-declared metadata
    boilerplate: list[str]  # excluded
    nested: list[str]  # subdir prefixes that are themselves structural datasets


def _split_boilerplate(paths: list[str]) -> tuple[list[str], list[str]]:
    boiler = [p for p in paths if posixpath.basename(p) in BOILERPLATE]
    rest = [p for p in paths if posixpath.basename(p) not in BOILERPLATE]
    return rest, boiler


def _has(paths: set[str], name: str) -> bool:
    return any(p == name or p.endswith("/" + name) for p in paths)


def detect(paths: list[str]) -> Detection:
    """Detect the structural kind of a flat list of relpaths (one composite root)."""
    rest, boiler = _split_boilerplate(paths)
    pset = set(rest)

    # --- LeRobot v2.x/v3.x: meta/info.json + data/chunk-*/*.parquet ---
    if _has(pset, "meta/info.json") and any(p.startswith("data/chunk-") for p in rest):
        meta = sorted(p for p in rest if p.startswith("meta/"))
        data = sorted(p for p in rest if p.startswith("data/") or p.startswith("videos/"))
        other = sorted(set(rest) - set(meta) - set(data))
        return Detection("lerobot", data, meta, boiler + other, [])

    # --- Zarr store: v2 (.zarray/.zgroup) or v3 (zarr.json) ---
    if any(posixpath.basename(p) in (".zarray", ".zgroup", "zarr.json") for p in rest):
        return Detection("zarr", sorted(rest), [], boiler, [])

    # --- Lance dataset: <name>.lance/ with _versions/ + data/ ---
    if any(p.endswith(".lance") or ".lance/" in ("/" + p) for p in rest) or _has(pset, "_versions"):
        return Detection("lance", sorted(rest), [], boiler, [])

    # --- TFDS / RLDS: dataset_info.json + features.json (+ *.tfrecord-*) ---
    if _has(pset, "dataset_info.json") and _has(pset, "features.json"):
        meta = sorted(
            p for p in rest if posixpath.basename(p) in ("dataset_info.json", "features.json")
        )
        data = sorted(set(rest) - set(meta))
        return Detection("rlds", data, meta, boiler, [])

    # --- WebDataset: a set of .tar shards ---
    tars = [p for p in rest if p.endswith(".tar")]
    if len(tars) >= 2 and len(tars) == len(rest):
        return Detection("webdataset", sorted(tars), [], boiler, [])

    # --- Flat parquet shards (climbmix / esm2 style) ---
    parquets = [p for p in rest if p.endswith(".parquet")]
    if len(parquets) >= 2:
        excluded = boiler + sorted(set(rest) - set(parquets))
        return Detection("parquet-shards", sorted(parquets), [], excluded, [])

    return Detection("unstructured", sorted(rest), [], boiler, [])


# Formats that declare themselves with a manifest/layout file are nestable as
# sub-datasets. Loose-shard formats (parquet-shards, webdataset) are deliberately
# excluded: a directory of partition dirs full of parquet shards (Hive-style
# ``date=.../part-*.parquet``) is ONE partitioned dataset, not nested sub-datasets,
# and is indistinguishable from a true container without a per-child manifest.
_NESTABLE_KINDS = frozenset({"lerobot", "zarr", "lance", "rlds"})


def detect_nested(paths: list[str]) -> Detection:
    """Detect a *container* of sub-datasets (the nesting case).

    Groups paths by top-level directory; if at least two top dirs each independently
    detect as a *manifest-bearing* structural dataset, returns kind ``"nested"`` with
    those dirs as nested composites. Otherwise falls back to flat :func:`detect` —
    so partitioned single datasets and loose-shard splits stay one flat composite.
    """
    rest, boiler = _split_boilerplate(paths)
    by_top: dict[str, list[str]] = {}
    for p in rest:
        if "/" in p:
            top, sub = p.split("/", 1)
            by_top.setdefault(top, []).append(sub)

    structural_children = [
        top for top, subs in by_top.items() if detect(subs).kind in _NESTABLE_KINDS
    ]

    if len(structural_children) >= 2:
        return Detection("nested", [], [], boiler, sorted(structural_children))
    return detect(paths)
