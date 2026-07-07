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
from typing import Any, Literal

from ...db.context import optional_repo
from .composite_builder import CompositeArtifactBuilder, CompositeLeaf


def build_view_edge(
    *,
    relation: str,
    anchor_digest: str,
    anchor_total: int,
    consumed_digests: list[str],
    component_algorithm: str = "sha256",
) -> dict[str, Any]:
    """Build a view-edge payload (a bloom over the consumed leaves).

    The bloom is keyed by ``{component_algorithm}:{digest}`` — ``sha256`` for HF datasets
    (crosswalk leaves) and ``blake3`` for local/``put``-registered datasets whose
    components are blake3-keyed. glaas-api tests membership with the same per-component
    algorithm, so either keying resolves.
    """
    leaves = [
        CompositeLeaf(
            relative_path="",
            digest=digest,
            size=0,
            component_type=None,
            component_algorithm=component_algorithm,
        )
        for digest in consumed_digests
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
        "selected_count": len(consumed_digests),
        "parent_total": anchor_total,
    }


def _blake3_digest(artifact: dict[str, Any]) -> str | None:
    """The artifact's blake3 hash (the native key of a local/``put`` composite leaf)."""
    for hash_row in artifact.get("hashes") or []:
        if hash_row.get("algorithm") == "blake3" and isinstance(hash_row.get("digest"), str):
            return hash_row["digest"]
    return None


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
    relation: Literal["consumes", "produces"] = "consumes",
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Resolve a job's leaf *hashes* into view edges + hashes to prune.

    Works from the collected-lineage hashes (not artifact ids). ``relation`` is the side
    being resolved: ``consumes`` over a job's input hashes (a run that read part of a
    dataset) or ``produces`` over its output hashes (a run that wrote one). The resolution
    is identical either way — each leaf is matched to the anchor composite(s) that carry
    it as a component. Returns ``(view_edges, prune_hashes, leaf_hashes)``:

    - ``prune_hashes``: the leaf hashes that collapse into a view edge plus any anchor
      composite hash linked as a plain input/output (the view edge replaces it). Used to
      drop these from a job's plain input/output edges.
    - ``leaf_hashes``: strictly the true component/leaf hashes subsumed into a view edge
      (a subset of ``prune_hashes`` that excludes a directly-linked anchor's own hash).
      A composite member never has its own session-scoped edge on GLaaS — it has no
      standing to carry its own labels/tags either, so callers use this set to keep
      per-leaf label/tag documents out of publish-time label sync.

    BOUNDARY: matching uses ``find_by_component_digest``, which only knows the anchor's
    *stored* components (capped at ``_MAX_STORED_COMPONENTS`` = 1000). For a dataset with
    more leaves than the cap, the tail beyond 1000 won't match here, so those leaf hashes
    are not pruned and remain as plain edges alongside the view edge. The bloom membership
    index covers the full set, but it is probabilistic (a ~0.1% false-positive rate would
    wrongly prune unrelated leaves), so it is deliberately NOT consulted for pruning.
    Tightening this for >1000-leaf datasets (exact pruning at formation time, where the
    full leaf set is known) is tracked as follow-up.
    """
    artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
    composites_repo: Any = optional_repo(db_ctx, "composites")
    if artifacts_repo is None or composites_repo is None:
        return [], set(), set()

    # anchor_id -> {"algorithm": str, "leaves": set, "shards": set}
    by_anchor: dict[str, dict[str, Any]] = {}
    prune: set[str] = set()

    def _attribute(leaf_digest: str, algorithm: str, input_digest: str) -> bool:
        matched = False
        for anchor_id in composites_repo.find_by_component_digest(leaf_digest, algorithm):
            bucket = by_anchor.setdefault(
                anchor_id, {"algorithm": algorithm, "leaves": set(), "shards": set()}
            )
            bucket["leaves"].add(leaf_digest)
            bucket["shards"].add(input_digest)
            matched = True
        return matched

    for digest in input_hashes:
        artifact = _get_by_any_hash(artifacts_repo, digest)
        if not artifact:
            continue
        # An anchor composite linked as a plain input (from attribution) is replaced by
        # the view edge.
        if _primary_composite_digest(artifact):
            prune.add(digest)
            continue
        # HF datasets: resolve the shard via its crosswalk sha256. Local/``put`` datasets:
        # the leaf is blake3-keyed and the run's input hash *is* that blake3 digest.
        sha256_digest = _origin_sha256(artifact)
        matched = _attribute(sha256_digest, "sha256", digest) if sha256_digest else False
        if not matched:
            blake3_digest = _blake3_digest(artifact)
            if blake3_digest:
                _attribute(blake3_digest, "blake3", digest)

    view_edges: list[dict[str, Any]] = []
    leaf_hashes: set[str] = set()
    for anchor_id, bucket in by_anchor.items():
        anchor = artifacts_repo.get(anchor_id)
        anchor_digest = _primary_composite_digest(anchor)
        if not anchor_digest:
            continue
        view_edges.append(
            build_view_edge(
                relation=relation,
                anchor_digest=anchor_digest,
                anchor_total=_anchor_total(anchor),
                consumed_digests=sorted(bucket["leaves"]),
                component_algorithm=bucket["algorithm"],
            )
        )
        prune |= bucket["shards"]
        leaf_hashes |= bucket["shards"]

    return view_edges, prune, leaf_hashes


def _primary_composite_digest(artifact: dict[str, Any] | None) -> str | None:
    """The anchor's ``composite-*`` digest (the view edge's target hash)."""
    if not artifact:
        return None
    for hash_row in artifact.get("hashes") or []:
        algorithm = str(hash_row.get("algorithm") or "")
        if algorithm.startswith("composite-") and isinstance(hash_row.get("digest"), str):
            return hash_row["digest"]
    return None
