"""Tests for the shared composite-formation core (`form_composites`).

`form_composites` is the single piece both `roar register` and `roar put` use to turn a
set of (path, blake3) items into composite artifacts. Policy:

* **auto (declared=False, the register path)** — composite ONLY self-declaring,
  manifest-bearing datasets (Zarr, LeRobot, RLDS, Lance + nested containers of those).
  The count-based shard heuristics (>=2 .parquet / >=2 .tar) do NOT form a dataset here:
  a later run reading a superset of parquets would be a different "dataset", so there's
  no stable identity.
* **declared (declared=True, the put path)** — the publish act declares the boundary, so
  loose-shard groupings under a declared root also composite.

It works purely from the supplied (path, hash) pairs — no filesystem access, no
re-hashing — so it is cheap to run over a run's recorded inputs/outputs at register time.
"""

from __future__ import annotations

import pytest

from roar.application.publish.form_composites import form_composites

SESSION = "s" * 64


def _items(*paths: str) -> list[tuple[str, str]]:
    """(path, deterministic-but-fake blake3) pairs — content need not exist on disk."""
    return [(p, _fake_hash(p)) for p in paths]


def _fake_hash(path: str) -> str:
    import hashlib

    return hashlib.blake2b(path.encode(), digest_size=32).hexdigest()


def _component_paths(result) -> set[str]:
    return {c["relative_path"] for c in result.payload.get("components", [])}


# ─────────────────────────── self-declaring (auto) datasets ───────────────────────────


def test_zarr_spanning_subdirs_forms_one_rooted_composite():
    items = _items(
        "data/climate.zarr/.zgroup",
        "data/climate.zarr/array/.zarray",
        "data/climate.zarr/array/.zattrs",
        "data/climate.zarr/array/0",
        "data/climate.zarr/array/1",
        "data/climate.zarr/array/2",
    )
    results = form_composites(items, session_hash=SESSION)
    assert len(results) == 1
    composite = results[0]
    # Rooted at the store dir, not the inner array dir.
    assert composite.root_path.rstrip("/").endswith("data/climate.zarr")
    # All the store's files are components (relative to the store root).
    assert _component_paths(composite) == {
        ".zgroup",
        "array/.zarray",
        "array/.zattrs",
        "array/0",
        "array/1",
        "array/2",
    }


def test_lerobot_forms_one_composite():
    items = _items(
        "out/robot/meta/info.json",
        "out/robot/meta/episodes.jsonl",
        "out/robot/data/chunk-000/episode_000000.parquet",
        "out/robot/data/chunk-000/episode_000001.parquet",
    )
    results = form_composites(items, session_hash=SESSION)
    assert len(results) == 1
    assert results[0].root_path.rstrip("/").endswith("out/robot")


# ─────────────────────────── the policy keystone: loose shards ───────────────────────────


def test_loose_parquets_are_not_a_dataset_unless_declared():
    items = _items(
        "data/shards/part_00000.parquet",
        "data/shards/part_00001.parquet",
        "data/shards/part_00002.parquet",
    )
    # auto: a directory of loose parquet a run happened to touch is NOT a dataset.
    assert form_composites(items, session_hash=SESSION, declared=False) == []
    # declared (put): the publish act declares the boundary, so it composites.
    declared = form_composites(items, session_hash=SESSION, declared=True)
    assert len(declared) == 1
    assert _component_paths(declared[0]) == {
        "part_00000.parquet",
        "part_00001.parquet",
        "part_00002.parquet",
    }


def test_loose_tars_webdataset_only_when_declared():
    items = _items("ds/a.tar", "ds/b.tar", "ds/c.tar")
    assert form_composites(items, session_hash=SESSION, declared=False) == []
    assert len(form_composites(items, session_hash=SESSION, declared=True)) == 1


# ─────────────────────────── multiple / nested / negative ───────────────────────────


def test_two_manifest_datasets_form_two_composites_loose_files_ignored():
    items = _items(
        "ds/a.zarr/.zgroup",
        "ds/a.zarr/0",
        "ds/b.zarr/.zgroup",
        "ds/b.zarr/0",
        "ds/notes.txt",  # loose, unstructured -> ignored (stays a primitive)
    )
    results = form_composites(items, session_hash=SESSION)
    roots = {r.root_path.rstrip("/").split("/")[-1] for r in results}
    assert roots == {"a.zarr", "b.zarr"}


def test_sibling_manifest_datasets_each_their_own_composite_no_auto_rollup():
    # Auto path: three independently-manifested Zarrs are three datasets. The "multi/ is
    # one container" rollup is a *declared* judgment (put), not an auto inference.
    items = _items(
        "multi/store_0.zarr/.zgroup",
        "multi/store_0.zarr/0",
        "multi/store_1.zarr/.zgroup",
        "multi/store_1.zarr/0",
        "multi/store_2.zarr/.zgroup",
        "multi/store_2.zarr/0",
    )
    results = form_composites(items, session_hash=SESSION)
    roots = {r.root_path.rstrip("/").split("/")[-1] for r in results}
    assert roots == {"store_0.zarr", "store_1.zarr", "store_2.zarr"}
    assert all(not r.root_path.rstrip("/").endswith("multi") for r in results)


def test_unstructured_pile_forms_nothing():
    items = _items("scratch/a.bin", "scratch/b.bin", "scratch/c.log")
    assert form_composites(items, session_hash=SESSION) == []


def test_single_file_forms_nothing():
    assert form_composites(_items("out/model.pt"), session_hash=SESSION) == []


# ─────────────────────────── identity properties ───────────────────────────


def test_boilerplate_excluded_from_identity():
    core = [
        "ds/x.zarr/.zgroup",
        "ds/x.zarr/array/.zarray",
        "ds/x.zarr/array/0",
        "ds/x.zarr/array/1",
    ]
    without = form_composites(_items(*core), session_hash=SESSION)[0]
    withbp = form_composites(
        _items(*core, "ds/x.zarr/README.md", "ds/x.zarr/.gitattributes"),
        session_hash=SESSION,
    )[0]
    # README/.gitattributes do not move the dataset identity.
    assert without.digest == withbp.digest


def test_pure_from_hashes_no_filesystem_access():
    # These paths do not exist on disk; formation must rely solely on supplied hashes.
    items = _items(
        "/nonexistent/x.zarr/.zgroup",
        "/nonexistent/x.zarr/array/.zarray",
        "/nonexistent/x.zarr/array/0",
        "/nonexistent/x.zarr/array/1",
    )
    results = form_composites(items, session_hash=SESSION)
    assert len(results) == 1


def test_idempotent_digest():
    items = _items("d/y.zarr/.zgroup", "d/y.zarr/0", "d/y.zarr/1")
    a = form_composites(items, session_hash=SESSION)[0]
    b = form_composites(list(reversed(items)), session_hash=SESSION)[0]
    assert a.digest == b.digest


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
