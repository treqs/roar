"""Per-dataset-type composite formation through the real publish boundary.

Drives ``build_publish_composite_results`` (the same path ``roar put`` uses) on
generated structural datasets and asserts each type forms a correct, path-sensitive
``composite-blake3`` composite with boilerplate excluded. This is the functional
checklist for structural-dataset support.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import blake3

from roar.application.publish.composite_builder import CompositeArtifactBuilder
from roar.application.publish.composites import build_publish_composite_results
from roar.application.publish.datasets import detect_additional_publish_composite_roots
from roar.application.publish.source_resolution import ResolvedSource


def _w(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _dir_sources(root: Path) -> tuple[list[ResolvedSource], dict[str, str]]:
    """Mimic a directory source: every file gets source_root=root (a declared dir)."""
    sources: list[ResolvedSource] = []
    hashes: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            sources.append(
                ResolvedSource(path=p, exists=True, relative_key=rel, source_root=root)
            )
            hashes[str(p)] = blake3.blake3(p.read_bytes()).hexdigest()
    return sources, hashes


def _composite_for_dir(root: Path):
    sources, hashes = _dir_sources(root)
    results = build_publish_composite_results(
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="s",
        source_type=None,
        additional_composite_roots={},
        composite_builder=CompositeArtifactBuilder(),
    )
    return results


# --- generators (minimal on-disk structure) --------------------------------
def _parquet_shards(root: Path, n: int = 5) -> Path:
    for i in range(n):
        _w(root / f"shard_{i:05d}.parquet", blake3.blake3(str(i).encode()).digest())
    _w(root / "README.md", b"# ds\n")
    _w(root / ".gitattributes", b"*.parquet filter=lfs -text\n")
    return root


def _lerobot(root: Path, episodes: int = 3) -> Path:
    for i in range(episodes):
        _w(root / "data" / "chunk-000" / f"episode_{i:06d}.parquet", bytes([i]) * 16)
    _w(root / "meta" / "info.json", json.dumps({"codebase_version": "v2.0", "fps": 30}).encode())
    _w(root / "meta" / "stats.json", b"{}")
    _w(root / "meta" / "episodes.jsonl", b'{"episode_index": 0}\n')
    _w(root / "meta" / "tasks.jsonl", b'{"task_index": 0}\n')
    _w(root / "README.md", b"# lr\n")
    return root


def _zarr_v3(root: Path) -> Path:
    _w(root / "zarr.json", b'{"zarr_format": 3, "node_type": "group"}\n')
    _w(root / "data" / "zarr.json", b'{"zarr_format": 3, "node_type": "array"}\n')
    for i in range(3):
        _w(root / "data" / "c" / str(i), bytes([i]) * 8)
    return root


def _webdataset(root: Path, n: int = 3) -> Path:
    for i in range(n):
        _w(root / f"{i:06d}.tar", bytes([i]) * 32)
    return root


# --- checklist -------------------------------------------------------------
def test_parquet_shards_form_composite_boilerplate_excluded(tmp_path: Path):
    results = _composite_for_dir(_parquet_shards(tmp_path / "ds", 5))
    assert len(results) == 1
    comp = results[0]
    paths = [c["relative_path"] for c in comp.payload["components"]]
    assert all(p.endswith(".parquet") for p in paths)
    assert "README.md" not in paths and ".gitattributes" not in paths
    assert comp.payload["hashes"][0]["algorithm"] == "composite-blake3"
    assert len(comp.digest) == 64


def test_lerobot_forms_composite_meta_in_boilerplate_out(tmp_path: Path):
    results = _composite_for_dir(_lerobot(tmp_path / "lr", 3))
    assert len(results) == 1
    paths = [c["relative_path"] for c in results[0].payload["components"]]
    assert any(p == "meta/info.json" for p in paths)  # meta is identity-bearing
    assert any(p.startswith("data/chunk-000/") for p in paths)
    assert "README.md" not in paths


def test_zarr_v3_forms_composite(tmp_path: Path):
    results = _composite_for_dir(_zarr_v3(tmp_path / "z"))
    assert len(results) == 1
    paths = [c["relative_path"] for c in results[0].payload["components"]]
    assert any(p.endswith("zarr.json") for p in paths)


def test_webdataset_forms_composite(tmp_path: Path):
    results = _composite_for_dir(_webdataset(tmp_path / "wds", 3))
    assert len(results) == 1
    assert all(c["relative_path"].endswith(".tar") for c in results[0].payload["components"])


def test_ungrouped_parquet_shards_are_structurally_grouped(tmp_path: Path):
    """Individual files (no source_root) group only when structurally a dataset."""
    root = _parquet_shards(tmp_path / "loose", 4)
    shard_files = sorted(root.glob("shard_*.parquet"))
    sources = [ResolvedSource(path=p, exists=True) for p in shard_files]
    roots = detect_additional_publish_composite_roots(resolved_sources=sources)
    assert root in roots and len(roots[root]) == 4


def test_two_unrelated_files_do_not_auto_composite(tmp_path: Path):
    """The removed n-vs-n+1 heuristic: random files under a dir must NOT composite."""
    _w(tmp_path / "misc" / "notes.txt", b"hi\n")
    _w(tmp_path / "misc" / "config.yaml", b"k: v\n")
    sources = [
        ResolvedSource(path=tmp_path / "misc" / "notes.txt", exists=True),
        ResolvedSource(path=tmp_path / "misc" / "config.yaml", exists=True),
    ]
    assert detect_additional_publish_composite_roots(resolved_sources=sources) == {}
