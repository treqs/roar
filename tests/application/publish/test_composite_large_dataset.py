"""A >1000-leaf composite resolves *every* leaf locally (the uncapped-local-index fix).

The GLaaS upload is bounded (payload size + paid storage) to <=1000 stored components +
a bloom. But locally roar keeps the FULL component set, so a partial reader of a leaf
beyond the upload cap still resolves to a consumes view edge and is pruned — the case the
old shared cap broke.
"""

from __future__ import annotations

from pathlib import Path

from roar.application.publish.composite_builder import (
    _MAX_STORED_COMPONENTS,
    CompositeArtifactBuilder,
    CompositeLeaf,
)
from roar.application.publish.view_edges import resolve_view_edges_for_job
from roar.db.context import create_database_context

_N = _MAX_STORED_COMPONENTS + 100  # > the upload cap, so there is a real tail
_TAIL = _MAX_STORED_COMPONENTS + 40  # a leaf the upload would NOT store


def _leaf(i: int) -> CompositeLeaf:
    return CompositeLeaf(
        relative_path=f"array/{i:05d}",
        digest=f"{i:064x}",
        size=8,
        component_type=None,
        leaf_kind="file",
        component_algorithm="blake3",
    )


def test_tail_leaf_beyond_upload_cap_resolves_and_prunes(tmp_path: Path):
    builder = CompositeArtifactBuilder()
    result = builder.build_for_leaves(
        root_path="data/big.zarr",
        leaves=[_leaf(i) for i in range(_N)],
        session_hash="",
        source_type=None,
    )
    assert result is not None
    # Local build keeps every component; the upload copy is capped.
    assert result.component_count_stored == _N
    assert (
        len(builder.cap_payload_for_upload(result.payload)["components"]) <= _MAX_STORED_COMPONENTS
    )

    tail_digest = f"{_TAIL:064x}"
    roar_dir = tmp_path / ".roar"
    with create_database_context(roar_dir) as ctx:
        # The anchor composite, persisted with the FULL component set.
        anchor_id, _ = ctx.artifacts.register(
            hashes={result.payload["hashes"][0]["algorithm"]: result.digest},
            size=0,
            path="data/big.zarr",
        )
        ctx.composites.upsert_details(
            artifact_id=anchor_id,
            components=result.payload["components"],
            component_count_total=result.component_count_total,
            membership_index=result.payload["membership_index"],
        )
        # A run consumed a single tail chunk (recorded as a plain input artifact).
        ctx.artifacts.register(hashes={"blake3": tail_digest}, size=8, path="data/big.zarr/array/x")

        edges, prune, leaf_hashes = resolve_view_edges_for_job(
            db_ctx=ctx, input_hashes=[tail_digest]
        )

    assert len(edges) == 1
    assert edges[0]["relation"] == "consumes"
    assert edges[0]["target_hash"] == result.digest
    assert edges[0]["parent_total"] == _N
    assert prune == {tail_digest}
    assert leaf_hashes == {tail_digest}
