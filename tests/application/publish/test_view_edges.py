"""Tests for view-edge construction + crosswalk resolution."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.application.publish.composite_builder import CompositeArtifactBuilder
from roar.application.publish.view_edges import build_view_edge, resolve_consumed_view_edges


def _bloom_member(bloom: dict, algorithm: str, digest: str) -> bool:
    builder = CompositeArtifactBuilder()
    raw = base64.b64decode(bloom["bloom_filter_base64"])
    bits, nhash = bloom["bloom_bits"], bloom["bloom_hashes"]
    h1, h2 = builder._bloom_hash_pair(f"{algorithm}:{digest}".encode())
    if h2 == 0:
        h2 = 1
    return all((raw[((h1 + i * h2) % bits) // 8] >> (((h1 + i * h2) % bits) % 8)) & 1 for i in range(nhash))


def test_build_view_edge_bloom_contains_consumed_leaves():
    a, b = "a" * 64, "b" * 64
    edge = build_view_edge(
        relation="consumes", anchor_digest="d" * 64, anchor_total=6543, consumed_digests=[a, b]
    )
    assert edge["relation"] == "consumes"
    assert edge["target_hash"] == "d" * 64
    assert edge["selected_count"] == 2
    assert edge["parent_total"] == 6543
    # The consumed leaves are members (keyed sha256); a non-consumed one is not.
    assert _bloom_member(edge["bloom"], "sha256", a) is True
    assert _bloom_member(edge["bloom"], "sha256", b) is True
    assert _bloom_member(edge["bloom"], "sha256", "c" * 64) is False
    # Wrong-algorithm key must miss (matches glaas-api's keying).
    assert _bloom_member(edge["bloom"], "blake3", a) is False


def test_resolve_consumed_view_edges_groups_by_anchor_and_prunes():
    shard_sha = "s" * 64
    db = SimpleNamespace(
        artifacts=MagicMock(),
        composites=MagicMock(),
    )
    db.artifacts.get.side_effect = lambda aid: {
        "shard-1": {
            "hashes": [{"algorithm": "blake3", "digest": "b" * 64}],
            "metadata": json.dumps({"origin": {"algorithm": "sha256", "digest": shard_sha}}),
        },
        "anchor-1": {
            "hashes": [{"algorithm": "composite-sha256", "digest": "d" * 64}],
            "component_count": 6543,
        },
    }.get(aid)
    db.composites.find_by_component_digest.side_effect = (
        lambda digest, algo="sha256": ["anchor-1"] if digest == shard_sha else []
    )

    edges, subsumed = resolve_consumed_view_edges(db_ctx=db, input_artifact_ids=["shard-1"])
    assert len(edges) == 1
    assert edges[0]["target_hash"] == "d" * 64
    assert edges[0]["parent_total"] == 6543
    assert edges[0]["selected_count"] == 1
    assert subsumed == {"shard-1"}
    assert _bloom_member(edges[0]["bloom"], "sha256", shard_sha) is True


def test_resolve_blake3_leaf_for_local_put_composite():
    """A run that consumes a leaf of a local/``put``-registered (blake3-tree) dataset —
    no sha256 crosswalk — resolves via the leaf's native blake3 key, and the view bloom
    is blake3-keyed (so glaas-api membership resolves)."""
    leaf_blake3 = "a" * 64
    db = SimpleNamespace(artifacts=MagicMock(), composites=MagicMock())
    db.artifacts.get.side_effect = lambda aid: {
        "leaf-1": {"hashes": [{"algorithm": "blake3", "digest": leaf_blake3}]},  # no origin sha256
        "zarr-anchor": {
            "hashes": [{"algorithm": "composite-blake3", "digest": "c" * 64}],
            "component_count": 8,
        },
    }.get(aid)
    db.composites.find_by_component_digest.side_effect = (
        lambda digest, algo="sha256": ["zarr-anchor"]
        if digest == leaf_blake3 and algo == "blake3"
        else []
    )

    edges, subsumed = resolve_consumed_view_edges(db_ctx=db, input_artifact_ids=["leaf-1"])
    assert len(edges) == 1
    assert edges[0]["target_hash"] == "c" * 64
    assert edges[0]["parent_total"] == 8
    assert subsumed == {"leaf-1"}
    # Bloom is keyed by blake3, not sha256.
    assert _bloom_member(edges[0]["bloom"], "blake3", leaf_blake3) is True
    assert _bloom_member(edges[0]["bloom"], "sha256", leaf_blake3) is False


def test_resolve_no_crosswalk_means_no_view_edges():
    db = SimpleNamespace(artifacts=MagicMock(), composites=MagicMock())
    db.artifacts.get.return_value = {"hashes": [{"algorithm": "blake3", "digest": "b" * 64}]}
    db.composites.find_by_component_digest.return_value = []
    edges, subsumed = resolve_consumed_view_edges(db_ctx=db, input_artifact_ids=["plain-1"])
    assert edges == []
    assert subsumed == set()


def _bloom_member_raw(bloom: dict, key: bytes) -> bool:
    builder = CompositeArtifactBuilder()
    raw = base64.b64decode(bloom["bloom_filter_base64"])
    bits, nhash = bloom["bloom_bits"], bloom["bloom_hashes"]
    h1, h2 = builder._bloom_hash_pair(key)
    if h2 == 0:
        h2 = 1
    return all((raw[((h1 + i * h2) % bits) // 8] >> (((h1 + i * h2) % bits) % 8)) & 1 for i in range(nhash))


def test_view_edge_bloom_no_false_negatives_and_bounded_false_positives():
    """Contract for the bloom-only view edge: every consumed leaf MUST be a member (no
    false negatives, ever), and the false-positive rate of the members scan stays within
    the configured 0.1% target (with margin). At the climbmix scale (100-of-thousands)
    the members endpoint is therefore a *superset* preview, not an exact list — exact
    selection comes from replaying the recorded command."""
    import hashlib

    from roar.application.publish.composite_builder import (
        _BLOOM_TARGET_FALSE_POSITIVE_RATE as TARGET,
    )

    for k in (100, 1000):
        consumed = [hashlib.sha256(f"leaf-{i}".encode()).hexdigest() for i in range(k)]
        edge = build_view_edge(
            relation="consumes", anchor_digest="d" * 64, anchor_total=k * 70, consumed_digests=consumed
        )
        bloom = edge["bloom"]
        # No false negatives: every consumed leaf is a member.
        assert all(_bloom_member_raw(bloom, f"sha256:{d}".encode()) for d in consumed)
        # False positives over a large disjoint non-member set stay under the target
        # (3x margin absorbs sampling noise; the floor only makes small K *better*).
        trials = 8000
        fp = sum(
            _bloom_member_raw(bloom, f"sha256:{hashlib.sha256(f'nonmember-{j}'.encode()).hexdigest()}".encode())
            for j in range(trials)
        )
        assert fp / trials <= TARGET * 3, f"K={k}: FP rate {fp/trials:.4f} exceeds {TARGET*3}"
