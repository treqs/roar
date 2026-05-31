"""Structural detection + identity-bearing file sets.

Detection is declared/structural, never scored. Fixtures are minimal on-disk
layouts (the detector only needs the structure, not loadable framework files).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from roar.application.composite import Leaf, detect, detect_nested, sha256_tree


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _relpaths(root: Path) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            out.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return sorted(out)


def _flat_parquet(root: Path, count: int = 4) -> Path:
    for i in range(count):
        _write(root / f"shard_{i:05d}.parquet", hashlib.sha256(str(i).encode()).digest())
    _write(root / "README.md", b"# ds\n")
    _write(root / ".gitattributes", b"*.parquet filter=lfs -text\n")
    return root


def _lerobot(root: Path, episodes: int = 3) -> Path:
    for i in range(episodes):
        _write(root / "data" / "chunk-000" / f"episode_{i:06d}.parquet", bytes([i]) * 16)
    _write(
        root / "meta" / "info.json", json.dumps({"codebase_version": "v2.0", "fps": 30}).encode()
    )
    _write(root / "meta" / "stats.json", b"{}")
    _write(root / "meta" / "episodes.jsonl", b'{"episode_index": 0}\n')
    _write(root / "meta" / "tasks.jsonl", b'{"task_index": 0}\n')
    _write(root / "README.md", b"# lr\n")
    return root


def _zarr_v2(root: Path) -> Path:
    _write(root / ".zgroup", b'{"zarr_format": 2}\n')
    _write(root / "array" / ".zarray", b'{"zarr_format": 2}\n')
    for i in range(3):
        _write(root / "array" / str(i), bytes([i]) * 8)
    return root


def _zarr_v3(root: Path) -> Path:
    _write(root / "zarr.json", b'{"zarr_format": 3, "node_type": "group"}\n')
    _write(root / "data" / "zarr.json", b'{"zarr_format": 3, "node_type": "array"}\n')
    for i in range(3):
        _write(root / "data" / "c" / str(i), bytes([i]) * 8)
    return root


def test_flat_parquet_shards(tmp_path: Path) -> None:
    d = detect(_relpaths(_flat_parquet(tmp_path / "ds", 5)))
    assert d.kind == "parquet-shards"
    assert len(d.data) == 5
    assert set(d.boilerplate) == {"README.md", ".gitattributes"}


def test_lerobot_meta_is_identity_boilerplate_is_not(tmp_path: Path) -> None:
    d = detect(_relpaths(_lerobot(tmp_path / "lr")))
    assert d.kind == "lerobot"
    assert "meta/info.json" in d.meta
    assert all(p.startswith("data/") for p in d.data)
    assert "README.md" in d.boilerplate


def test_zarr_v2_and_v3(tmp_path: Path) -> None:
    assert detect(_relpaths(_zarr_v2(tmp_path / "z2"))).kind == "zarr"
    assert detect(_relpaths(_zarr_v3(tmp_path / "z3"))).kind == "zarr"


def test_single_file_is_unstructured(tmp_path: Path) -> None:
    _write(tmp_path / "m" / "model.safetensors", b"x" * 32)
    d = detect(_relpaths(tmp_path / "m"))
    assert d.kind == "unstructured" and len(d.data) == 1


def test_nested_container(tmp_path: Path) -> None:
    _zarr_v2(tmp_path / "c" / "store_a.zarr")
    _zarr_v3(tmp_path / "c" / "store_b.zarr")
    d = detect_nested(_relpaths(tmp_path / "c"))
    assert d.kind == "nested"
    assert len(d.nested) == 2


def test_end_to_end_meta_edit_moves_hash_readme_does_not(tmp_path: Path) -> None:
    root = _lerobot(tmp_path / "lr")

    def key() -> str:
        d = detect(_relpaths(root))
        kinds = (
            dict.fromkeys(d.data, "data")
            | dict.fromkeys(d.meta, "meta")
            | dict.fromkeys(d.boilerplate, "boilerplate")
        )
        leaves = [
            Leaf(
                r,
                hashlib.sha256((root / r).read_bytes()).hexdigest(),
                kind=kinds.get(r, "data"),
                verified=True,
            )
            for r in _relpaths(root)
        ]
        return sha256_tree(leaves)

    base = key()
    (root / "README.md").write_bytes(b"# different readme bytes\n")
    assert key() == base
    (root / "meta" / "info.json").write_bytes(b'{"codebase_version": "v2.1", "fps": 60}')
    assert key() != base
