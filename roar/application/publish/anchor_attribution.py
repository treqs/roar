"""Materialize job -> dataset-anchor provenance edges from the local crosswalk.

When `roar get` imports an HF dataset it forms a full-dataset ``sha256-tree`` anchor
and stores each downloaded shard as a local artifact carrying both its ``blake3``
(the rename-stable local identity) and its ``sha256`` (the HF LFS oid, the anchor's
leaf identity) — the local crosswalk cache. A later ``roar run`` that reads a shard
records it as an input by ``blake3``, so on its own the lineage graph cannot see that
the run consumed data belonging to the dataset.

This module closes that gap at register time: for each job input that carries a
crosswalk ``sha256`` matching a stored leaf of a known anchor, it materializes a
``job -> anchor`` input edge. The bridge is at the composite/job level — it never
publishes the ``blake3 <-> sha256`` pair to GLaaS — so the dataset shows up as an
input of the run ("what dataset trained this model") while the hash planes stay
separate. The edge is a true provenance fact, so it is persisted locally and the
existing register machinery propagates it to GLaaS unchanged.
"""

from __future__ import annotations

from typing import Any

from ...core.interfaces.logger import ILogger
from ...db.context import optional_repo


def attribute_jobs_to_anchors(*, db_ctx: Any, job_ids: list[int], logger: ILogger) -> int:
    """Add ``job -> anchor`` input edges for crosswalk-resolved dataset membership.

    Returns the number of edges added (0 when nothing resolves), so the caller can
    decide whether the collected lineage needs to be re-read.
    """
    artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
    composites_repo: Any = optional_repo(db_ctx, "composites")
    jobs_repo: Any = optional_repo(db_ctx, "jobs")
    if artifacts_repo is None or composites_repo is None or jobs_repo is None:
        return 0

    added = 0
    for job_id in job_ids:
        existing_inputs = jobs_repo.get_inputs(job_id)
        existing_artifact_ids = {
            inp.get("artifact_id") for inp in existing_inputs if inp.get("artifact_id")
        }
        resolved_anchors: dict[str, str] = {}  # anchor_artifact_id -> path label

        for inp in existing_inputs:
            input_artifact_id = inp.get("artifact_id")
            if not input_artifact_id:
                continue
            artifact = artifacts_repo.get(input_artifact_id)
            if not artifact:
                continue
            for hash_row in artifact.get("hashes", []):
                if hash_row.get("algorithm") != "sha256":
                    continue
                sha256_digest = hash_row.get("digest")
                if not sha256_digest:
                    continue
                for anchor_id in composites_repo.find_by_component_digest(sha256_digest, "sha256"):
                    # Never attribute an input to itself, to an already-linked input,
                    # or to an anchor resolved earlier in this pass.
                    if (
                        anchor_id == input_artifact_id
                        or anchor_id in existing_artifact_ids
                        or anchor_id in resolved_anchors
                    ):
                        continue
                    anchor = artifacts_repo.get(anchor_id)
                    if not anchor:
                        continue
                    resolved_anchors[anchor_id] = (
                        anchor.get("first_seen_path") or anchor.get("path") or anchor_id
                    )

        for anchor_id, anchor_path in resolved_anchors.items():
            jobs_repo.add_input(job_id, anchor_id, anchor_path)
            added += 1
            logger.debug("Attributed job %s -> anchor %s", job_id, anchor_id)

    return added
