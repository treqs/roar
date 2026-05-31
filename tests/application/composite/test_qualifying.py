"""Qualifying composite hash (``sha256-tree``) invariants.

Mirrors the compspec oracle in ~/comp-art-e2e. If these drift, the dataset key
contract is broken.
"""

from __future__ import annotations

import pytest

from roar.application.composite import (
    Leaf,
    blake3_tree,
    build,
    build_nested,
    normalize_relpath,
    preimage,
    sha256_tree,
    tree,
)


def _leaves(*pairs: tuple[str, str], kind: str = "data") -> list[Leaf]:
    return [Leaf(p, h, kind=kind) for p, h in pairs]


def test_deterministic_and_order_independent() -> None:
    a = _leaves(("b.parquet", "11" * 32), ("a.parquet", "22" * 32))
    b = _leaves(("a.parquet", "22" * 32), ("b.parquet", "11" * 32))
    assert sha256_tree(a) == sha256_tree(b)
    assert sha256_tree(a) == sha256_tree(a)


def test_path_sensitive() -> None:
    before = _leaves(("train/000.parquet", "aa" * 32), ("validation/000.parquet", "bb" * 32))
    after = _leaves(("validation/000.parquet", "aa" * 32), ("train/000.parquet", "bb" * 32))
    assert sha256_tree(before) != sha256_tree(after)


def test_content_sensitive() -> None:
    assert sha256_tree(_leaves(("a", "aa" * 32))) != sha256_tree(_leaves(("a", "bb" * 32)))


def test_rename_changes_hash_even_same_bytes() -> None:
    assert sha256_tree(_leaves(("a", "aa" * 32))) != sha256_tree(_leaves(("b", "aa" * 32)))


def test_boilerplate_excluded_meta_included() -> None:
    data = _leaves(("data/x", "aa" * 32))
    with_boiler = [*data, Leaf("README.md", "ff" * 32, kind="boilerplate")]
    assert sha256_tree(data) == sha256_tree(with_boiler)

    m1 = [*data, Leaf("meta/info.json", "11" * 32, kind="meta")]
    m2 = [*data, Leaf("meta/info.json", "22" * 32, kind="meta")]
    assert sha256_tree(m1) != sha256_tree(m2)


def test_single_algorithm_is_sha256() -> None:
    pre = preimage([("a.parquet", "sha256", "aa" * 32)])
    assert b"sha256:" in pre
    assert b"blake3:" not in pre and b"git-sha1:" not in pre
    assert pre.startswith(b"roar:sha256-tree:v1\n")


def test_asserted_vs_verified_same_digest() -> None:
    asserted = [Leaf("a", "aa" * 32, verified=False)]
    verified = [Leaf("a", "aa" * 32, verified=True)]
    assert sha256_tree(asserted) == sha256_tree(verified)
    assert build(asserted).verified is False
    assert build(verified).verified is True


def test_duplicate_and_escape_are_errors() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        sha256_tree(_leaves(("a", "aa" * 32), ("./a", "bb" * 32)))
    with pytest.raises(ValueError, match="escapes"):
        normalize_relpath("../secret")


def test_path_normalization() -> None:
    assert normalize_relpath("./a/b") == "a/b"
    assert normalize_relpath("/a/b") == "a/b"
    assert normalize_relpath("a\\b") == "a/b"


def test_nested_composite_of_composites() -> None:
    child_a = build(_leaves(("x", "aa" * 32)))
    child_b = build(_leaves(("y", "bb" * 32)))
    parent = build_nested([("store_a.zarr", child_a), ("store_b.zarr", child_b)])
    parent2 = build_nested([("store_b.zarr", child_b), ("store_a.zarr", child_a)])
    assert parent.digest == parent2.digest
    swapped = build_nested([("store_a.zarr", child_b), ("store_b.zarr", child_a)])
    assert parent.digest != swapped.digest


def test_empty_is_defined_and_stable() -> None:
    assert sha256_tree([]) == sha256_tree([])


def test_algorithm_follows_source_blake3_vs_sha256() -> None:
    """Same leaves, different content algorithm -> different key (domain-separated)."""
    leaves = _leaves(("data/x", "aa" * 32), ("data/y", "bb" * 32))
    assert tree(leaves, algo="sha256") == sha256_tree(leaves)
    assert tree(leaves, algo="blake3") == blake3_tree(leaves)
    assert sha256_tree(leaves) != blake3_tree(leaves)  # per-algo domain + combiner


def test_build_labels_algo_and_is_path_sensitive_per_algo() -> None:
    leaves = _leaves(("train/0", "aa" * 32), ("val/0", "bb" * 32))
    sha = build(leaves, algo="sha256")
    bl = build(leaves, algo="blake3")
    assert sha.algo == "sha256-tree" and bl.algo == "blake3-tree"
    moved = _leaves(("val/0", "aa" * 32), ("train/0", "bb" * 32))
    assert build(moved, algo="blake3").digest != bl.digest  # path-sensitive under blake3
