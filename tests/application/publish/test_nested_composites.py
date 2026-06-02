"""Nested composite-of-composites at the publish boundary.

A directory that contains >=2 independently-structured sub-datasets (e.g. the
openvla/modified_libero_rlds layout: several RLDS task dirs) forms a parent
composite whose hash is Merkle over the child composite hashes and whose membership
bloom is flat over every leaf blob.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import blake3

from roar.application.composite import Leaf
from roar.application.publish.composite_builder import CompositeArtifactBuilder
from roar.application.publish.source_resolution import ResolvedSource


def _w(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sources(root: Path):
    sources, hashes = [], {}
    for dp, _d, files in os.walk(root):
        for f in files:
            p = Path(dp) / f
            rel = p.relative_to(root).as_posix()
            sources.append(ResolvedSource(path=p, exists=True, relative_key=rel, source_root=root))
            hashes[str(p)] = blake3.blake3(p.read_bytes()).hexdigest()
    return sources, hashes


def _build_all(root: Path):
    sources, hashes = _sources(root)
    return CompositeArtifactBuilder().build_all_for_root(
        root_path=root,
        resolved_sources=sources,
        hashes_by_path=hashes,
        session_hash="s",
        source_type=None,
    )


def _rlds_task(root: Path, shards: int = 2) -> None:
    """An RLDS-shaped sub-dataset: dataset_info.json + features.json + tfrecords."""
    _w(root / "dataset_info.json", b'{"name":"t"}')
    _w(root / "features.json", b'{"obs":"image"}')
    for i in range(shards):
        _w(root / f"data.tfrecord-{i:05d}-of-{shards:05d}", bytes([i]) * 64)


def test_container_of_rlds_tasks_forms_parent_and_children(tmp_path: Path):
    root = tmp_path / "modified_libero_rlds"
    for task in ["libero_10", "libero_goal", "libero_object"]:
        _rlds_task(root / task)
    _w(root / "README.md", b"# libero\n")

    results = _build_all(root)
    assert len(results) == 4  # 3 children + 1 parent

    parent = results[-1]
    children = results[:-1]
    assert parent.root_path == str(root)
    # parent components are the child composites, by sub-dir, marked leaf_kind=composite
    comp = {c["relative_path"]: c for c in parent.payload["components"]}
    assert set(comp) == {"libero_10", "libero_goal", "libero_object"}
    assert all(c["leaf_kind"] == "composite" for c in comp.values())
    # each parent component digest == the corresponding child composite digest
    child_by_root = {Path(c.root_path).name: c for c in children}
    for name, c in comp.items():
        assert c["component_digest"] == child_by_root[name].digest


def test_parent_hash_is_merkle_over_child_hashes(tmp_path: Path):
    root = tmp_path / "c"
    for task in ["a_task", "b_task"]:
        _rlds_task(root / task)
    results = _build_all(root)
    parent, children = results[-1], results[:-1]

    # reference: Merkle = blake3-tree over (child_dir, child_algo, child_digest)
    ref_leaves = [
        Leaf(Path(c.root_path).name, c.digest, kind="data")
        for c in sorted(children, key=lambda c: Path(c.root_path).name)
    ]
    # parent uses each child's own algorithm tag in the preimage; children are
    # composite-blake3, so the merkle leaf algo is composite-blake3 (not blake3).
    from roar.application.composite.canonical import preimage

    triples = [
        (Path(c.root_path).name, c.payload["hashes"][0]["algorithm"], c.digest)
        for c in sorted(children, key=lambda c: Path(c.root_path).name)
    ]
    expected = blake3.blake3(preimage(triples, domain=b"roar:blake3-tree:v1\n")).hexdigest()
    assert parent.digest == expected
    assert parent.payload["hashes"][0]["algorithm"] == "composite-blake3"
    assert ref_leaves  # sanity


def test_parent_bloom_is_flat_over_leaf_blobs_not_child_hashes(tmp_path: Path):
    root = tmp_path / "c"
    for task in ["a_task", "b_task"]:
        _rlds_task(root / task, shards=2)
    results = _build_all(root)
    parent = results[-1]

    membership = parent.payload["membership_index"]
    bloom = bytearray(base64.b64decode(membership["bloom_filter_base64"]))
    bits, nhash = membership["bloom_bits"], membership["bloom_hashes"]

    def contains(key: bytes) -> bool:
        seed = blake3.blake3(b"roar:composite-membership:v1\0" + key).digest()
        h1 = int.from_bytes(seed[0:8], "little")
        h2 = int.from_bytes(seed[8:16], "little") or 1
        return all(
            (bloom[(h1 + i * h2) % bits // 8] >> ((h1 + i * h2) % bits % 8)) & 1
            for i in range(nhash)
        )

    # every leaf blob (across both children) is in the parent bloom...
    for dp, _d, files in os.walk(root):
        for f in files:
            if f in ("README.md", ".gitattributes"):
                continue
            digest = blake3.blake3((Path(dp) / f).read_bytes()).hexdigest()
            assert contains(f"blake3:{digest}".encode())
    # ...but a child COMPOSITE hash is NOT a bloom member (keys are blobs only)
    assert not contains(f"composite-blake3:{results[0].digest}".encode())


def test_parent_payload_satisfies_glaas_count_invariants(tmp_path: Path):
    """GLaaS requires membership.total_components == component_count_total and
    membership.stored_components == len(components). The parent tracks blob count as
    the total and child count as stored."""
    root = tmp_path / "c"
    for task in ["a_task", "b_task"]:
        _rlds_task(root / task, shards=2)
    parent = _build_all(root)[-1]
    p = parent.payload
    mi = p["membership_index"]
    n_children = len(p["components"])
    assert mi["total_components"] == p["component_count_total"]  # GLaaS invariant
    assert mi["stored_components"] == n_children  # GLaaS invariant
    assert mi["stored_components"] <= mi["total_components"]
    assert n_children <= p["component_count_total"]
    # total tracks blob population, stored tracks the child composites
    assert p["component_count_total"] > n_children


def test_single_structural_child_does_not_nest(tmp_path: Path):
    """One sub-dataset under a dir is not a container — stays a flat composite."""
    root = tmp_path / "c"
    _rlds_task(root / "only_task")
    results = _build_all(root)
    assert len(results) == 1
    assert results[0].root_path == str(root)


def test_partitioned_single_dataset_does_not_nest(tmp_path: Path):
    """Hive-style partitions are ONE dataset, not nested sub-datasets.

    Each ``date=.../part-*.parquet`` dir detects as parquet-shards (loose shards,
    no manifest) which is NOT nestable, so the container stays a single flat
    composite rather than a composite-of-composites.
    """
    root = tmp_path / "events"
    for day in ["date=2026-01-01", "date=2026-01-02"]:
        _w(root / day / "part-00000.parquet", day.encode())
        _w(root / day / "part-00001.parquet", (day + "b").encode())
    results = _build_all(root)
    assert len(results) == 1  # one flat composite, not nested
    assert results[0].payload["hashes"][0]["algorithm"] == "composite-blake3"
