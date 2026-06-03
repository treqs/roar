"""Form composites from a run's recorded inputs/outputs at register time.

`roar run` records the files a job read and wrote as loose ``blake3`` leaves; it does
not roll structured datasets (a Zarr store, a LeRobot dataset, ...) up into a composite.
This module closes that gap at ``roar register`` time, symmetrically with how
``anchor_attribution`` materializes job->dataset edges from the get crosswalk.

For each job it runs the shared :func:`form_composites` core over the job's leaf inputs
and (separately) its leaf outputs, persists any detected dataset as a local composite
artifact (``kind=composite`` via :meth:`composites.upsert_details`), and links it to the
job — a produced dataset becomes an output edge, a consumed one an input edge. The
register flow then re-collects the lineage, so the existing bundle-registration and
view-edge machinery surface the composite unchanged (a partial reader of the same
dataset resolves a ``consumes`` view edge against these components).

Only self-declaring, manifest-bearing structures are formed here (``declared=False`` —
see the form_composites policy): a directory of loose parquet a run happened to touch is
deliberately NOT a dataset. The only byte-free path: it works purely from the recorded
``blake3`` digests, so it is O(files) per job.
"""

from __future__ import annotations

from typing import Any

from ...core.interfaces.logger import ILogger
from ...db.context import optional_repo
from .form_composites import form_composites
from .metadata import build_local_publish_composite_metadata_json


def form_lineage_composites(*, db_ctx: Any, job_ids: list[int], logger: ILogger) -> int:
    """Form + persist + link composites for each job's leaf inputs and outputs.

    Returns the number of new job<->composite edges added, so the caller can decide
    whether the collected lineage needs to be re-read (mirrors ``attribute_jobs_to_anchors``).
    """
    artifacts_repo: Any = optional_repo(db_ctx, "artifacts")
    composites_repo: Any = optional_repo(db_ctx, "composites")
    jobs_repo: Any = optional_repo(db_ctx, "jobs")
    if artifacts_repo is None or composites_repo is None or jobs_repo is None:
        return 0

    added = 0
    for job_id in job_ids:
        for relation, rows in (
            ("input", jobs_repo.get_inputs(job_id)),
            ("output", jobs_repo.get_outputs(job_id)),
        ):
            linked_ids = {r.get("artifact_id") for r in rows}
            items = _leaf_items(rows)
            if len(items) < 2:
                continue
            for composite in form_composites(items, session_hash="", declared=False):
                artifact_id = _persist_composite(artifacts_repo, composites_repo, composite, logger)
                if artifact_id is None or artifact_id in linked_ids:
                    continue
                if relation == "output":
                    jobs_repo.add_output(job_id, artifact_id, composite.root_path)
                else:
                    jobs_repo.add_input(job_id, artifact_id, composite.root_path)
                linked_ids.add(artifact_id)
                added += 1
                logger.debug(
                    "Formed %s composite %s for job %s",
                    relation,
                    composite.digest[:12],
                    job_id,
                )
    return added


def prune_composite_output_leaves(*, db_ctx: Any, lineage_jobs: list[Any], logger: ILogger) -> int:
    """Drop loose leaves subsumed by a composite output from the registration bundle.

    Mirrors how view-edge prep prunes consumed leaf *inputs*: a producer job that wrote a
    structured dataset should register the composite as its output, not the dataset as a
    composite PLUS its loose leaves. Operates on the collected bundle (``job["_outputs"]``
    / ``job["_output_hashes"]``) only — the local DB record stays complete. A leaf is
    subsumed when its output path falls under a composite output's root.
    """
    jobs_repo: Any = optional_repo(db_ctx, "jobs")
    if jobs_repo is None:
        return 0

    pruned = 0
    for job in lineage_jobs:
        if not isinstance(job, dict):
            continue
        job_id = _job_db_id(job)
        if job_id is None:
            continue
        rows = jobs_repo.get_outputs(job_id)
        roots = [r.get("path") for r in rows if r.get("kind") == "composite" and r.get("path")]
        if not roots:
            continue
        subsumed = {
            r.get("artifact_hash")
            for r in rows
            if r.get("kind") != "composite"
            and r.get("artifact_hash")
            and any(_path_under(r.get("path") or "", root) for root in roots)
        }
        if not subsumed:
            continue
        before = len(job.get("_output_hashes") or [])
        job["_output_hashes"] = [h for h in (job.get("_output_hashes") or []) if h not in subsumed]
        if isinstance(job.get("_outputs"), list):
            job["_outputs"] = [o for o in job["_outputs"] if o.get("hash") not in subsumed]
        removed = before - len(job["_output_hashes"])
        if removed:
            pruned += removed
            logger.debug("Pruned %d subsumed leaf outputs from job %s", removed, job_id)
    return pruned


def _job_db_id(job: dict[str, Any]) -> int | None:
    raw_id = job.get("id")
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, str) and raw_id.isdigit():
        return int(raw_id)
    return None


def _path_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _leaf_items(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(path, blake3)`` pairs for the leaf (non-composite) rows of a job edge set."""
    items: list[tuple[str, str]] = []
    for row in rows:
        if row.get("kind") == "composite":
            continue
        digest = _leaf_blake3(row)
        path = row.get("path") or row.get("first_seen_path")
        if digest and path:
            items.append((str(path), digest))
    return items


def _leaf_blake3(row: dict[str, Any]) -> str | None:
    for hash_row in row.get("hashes") or []:
        if hash_row.get("algorithm") == "blake3" and isinstance(hash_row.get("digest"), str):
            return hash_row["digest"]
    return None


def _persist_composite(
    artifacts_repo: Any,
    composites_repo: Any,
    composite: Any,
    logger: ILogger,
) -> str | None:
    """Persist a formed composite locally (artifact row + composite details)."""
    try:
        payload_hashes = composite.payload.get("hashes") or []
        algorithm = "composite-blake3"
        if payload_hashes and isinstance(payload_hashes[0], dict):
            algorithm = payload_hashes[0].get("algorithm") or algorithm
        artifact_id, _created = artifacts_repo.register(
            hashes={algorithm: composite.digest},
            size=int(composite.payload.get("size") or 0),
            path=composite.root_path,
            source_type=composite.payload.get("source_type"),
            metadata=build_local_publish_composite_metadata_json(
                composite=composite, dataset_identifiers=None
            ),
        )
        composites_repo.upsert_details(
            artifact_id=artifact_id,
            components=list(composite.payload.get("components") or []),
            component_count_total=composite.component_count_total,
            membership_index=composite.payload.get("membership_index"),
        )
        return str(artifact_id)
    except Exception as exc:  # pragma: no cover - defensive best effort
        logger.warning(
            "Register-time composite formation failed for %s: %s",
            composite.digest[:12],
            exc,
        )
        return None
