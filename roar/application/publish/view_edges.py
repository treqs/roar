"""Build view edges from a job's crosswalk-resolved inputs.

A *view edge* records that a job consumed (or produced) a bloom-described selection of
a composite anchor — the bloom over the leaves it actually touched, keyed by their
origin algorithm (``sha256`` for HF). The exact subset stays recoverable (the anchor's
leaves x the bloom) and replayable (the recorded command), so only the bloom is sent.

This replaces the Phase-4 "link the anchor as a plain input alongside the shards"
behaviour: the job links one ``consumes`` view edge to the anchor and the per-shard
input edges collapse into it.
"""

from __future__ import annotations

import json
from typing import Any

from ...db.context import optional_repo
from .composite_builder import CompositeArtifactBuilder, CompositeLeaf


def build_view_edge(
    *,
    relation: str,
    anchor_digest: str,
    anchor_total: int,
    consumed_sha256: list[str],
) -> dict[str, Any]:
    """Build a view-edge payload (a bloom over the consumed sha256 leaves)."""
    leaves = [
        CompositeLeaf(
            relative_path="",
            digest=digest,
            size=0,
            component_type=None,
            component_algorithm="sha256",
        )
        for digest in consumed_sha256
    ]
    membership = CompositeArtifactBuilder()._build_membership_index_base(leaves)
    return {
        "relation": relation,
        "target_hash": anchor_digest,
        "bloom": {
            "bloom_filter_base64": membership["bloom_filter_base64"],
            "bloom_bits": membership["bloom_bits"],
            "bloom_hashes": membership["bloom_hashes"],
            "bloom_version": membership["bloom_version"],
        },
        "selected_count": len(consumed_sha256),
        "parent_total": anchor_total,
    }


def _origin_sha256(artifact: dict[str, Any]) -> str | None:
    """The shard's origin sha256 from the local crosswalk metadata (see F1)."""
    raw = artifact.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            origin = (json.loads(raw) or {}).get("origin") or {}
        except (ValueError, TypeError):
            origin = {}
        if origin.get("algorithm") == "sha256" and isinstance(origin.get("digest"), str):
            return origin["digest"]
    for hash_row in artifact.get("hashes") or []:
        if hash_row.get("algorithm") == "sha256" and isinstance(hash_row.get("digest"), str):
            return hash_row["digest"]
    return None


def _anchor_total(artifact: dict[str, Any] | None) -> int:
    """The anchor's full component count (for ``parent_total``)."""
    if not artifact:
        return 0
    count = artifact.get("component_count")
    if isinstance(count, int) and count > 0:
        return count
    raw = artifact.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            composite = (json.loads(raw) or {}).get("composite") or {}
        except (ValueError, TypeError):
            composite = {}
        total = composite.get("component_count_total")
        if isinstance(total, int):
            return total
    return 0


def resolve_consumed_view_edges(
    *,
    db_ctx: Any,
    input_artifact_ids: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Resolve a job's crosswalk inputs into ``consumes`` view edges.

    Groups the inputs that carry a crosswalk sha256 by the anchor whose stored
    components include that sha256, builds one view-edge bloom per anchor over the
    consumed leaves, and returns ``(view_edges, subsumed_artifact_ids)`` — the second
    being the input artifact ids that collapse into a view edge and should be pruned
    from the plain inputs.
    """
    artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
    composites_repo: Any = optional_repo(db_ctx, "composites")
    if artifacts_repo is None or composites_repo is None:
        return [], set()

    # anchor_id -> {"sha256": set, "subsumed": set of input artifact ids}
    by_anchor: dict[str, dict[str, set[str]]] = {}
    for artifact_id in input_artifact_ids:
        artifact = artifacts_repo.get(artifact_id)
        if not artifact:
            continue
        sha256_digest = _origin_sha256(artifact)
        if not sha256_digest:
            continue
        anchors = composites_repo.find_by_component_digest(sha256_digest, "sha256")
        for anchor_id in anchors:
            if anchor_id == artifact_id:
                continue
            bucket = by_anchor.setdefault(anchor_id, {"sha256": set(), "subsumed": set()})
            bucket["sha256"].add(sha256_digest)
            bucket["subsumed"].add(artifact_id)

    view_edges: list[dict[str, Any]] = []
    subsumed: set[str] = set()
    for anchor_id, bucket in by_anchor.items():
        anchor = artifacts_repo.get(anchor_id)
        anchor_digest = _primary_composite_digest(anchor)
        if not anchor_digest:
            continue
        view_edges.append(
            build_view_edge(
                relation="consumes",
                anchor_digest=anchor_digest,
                anchor_total=_anchor_total(anchor),
                consumed_sha256=sorted(bucket["sha256"]),
            )
        )
        subsumed |= bucket["subsumed"]

    return view_edges, subsumed


def _get_by_any_hash(artifacts_repo: Any, digest: str) -> dict[str, Any] | None:
    for algorithm in ("blake3", "composite-sha256", "composite-blake3", "sha256"):
        artifact = artifacts_repo.get_by_hash(digest, algorithm=algorithm)
        if artifact:
            return artifact
    return artifacts_repo.get_by_hash(digest)


def resolve_view_edges_for_job(
    *,
    db_ctx: Any,
    input_hashes: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Resolve a job's input *hashes* into ``consumes`` view edges + hashes to prune.

    Works from the collected-lineage hashes (not artifact ids). Returns
    ``(view_edges, prune_hashes)`` — the second being input hashes that collapse into a
    view edge (the consumed shards) plus any anchor composite hash linked as a plain
    input (the view edge replaces it).
    """
    artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
    composites_repo: Any = optional_repo(db_ctx, "composites")
    if artifacts_repo is None or composites_repo is None:
        return [], set()

    by_anchor: dict[str, dict[str, set[str]]] = {}
    prune: set[str] = set()
    for digest in input_hashes:
        artifact = _get_by_any_hash(artifacts_repo, digest)
        if not artifact:
            continue
        # An anchor composite linked as a plain input (from attribution) is replaced by
        # the view edge.
        if _primary_composite_digest(artifact):
            prune.add(digest)
            continue
        sha256_digest = _origin_sha256(artifact)
        if not sha256_digest:
            continue
        for anchor_id in composites_repo.find_by_component_digest(sha256_digest, "sha256"):
            bucket = by_anchor.setdefault(anchor_id, {"sha256": set(), "shards": set()})
            bucket["sha256"].add(sha256_digest)
            bucket["shards"].add(digest)

    view_edges: list[dict[str, Any]] = []
    for anchor_id, bucket in by_anchor.items():
        anchor = artifacts_repo.get(anchor_id)
        anchor_digest = _primary_composite_digest(anchor)
        if not anchor_digest:
            continue
        view_edges.append(
            build_view_edge(
                relation="consumes",
                anchor_digest=anchor_digest,
                anchor_total=_anchor_total(anchor),
                consumed_sha256=sorted(bucket["sha256"]),
            )
        )
        prune |= bucket["shards"]

    return view_edges, prune


def _primary_composite_digest(artifact: dict[str, Any] | None) -> str | None:
    """The anchor's ``composite-*`` digest (the view edge's target hash)."""
    if not artifact:
        return None
    for hash_row in artifact.get("hashes") or []:
        algorithm = str(hash_row.get("algorithm") or "")
        if algorithm.startswith("composite-") and isinstance(hash_row.get("digest"), str):
            return hash_row["digest"]
    return None
