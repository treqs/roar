"""
Lineage collector service for upload operations.

Extracted from put.py to follow Single Responsibility Principle.
This service collects all lineage data (jobs, artifacts) needed
for registering artifacts with GLaaS.
"""

from pathlib import Path

from ...core.digests import extract_primary_digest
from ...core.interfaces.upload import LineageData
from ...db.context import create_database_context


def compute_io_signature(job: dict) -> str:
    """
    Compute signature from sorted input/output hashes.

    Used to identify re-runs of the same logical step.
    A job X is a re-run of job Y if they have identical inputs and outputs.

    Jobs with no inputs or outputs use job_uid as signature since
    we cannot determine re-run relationships from artifacts alone.
    """
    inputs = tuple(sorted(job.get("_input_hashes", [])))
    outputs = tuple(sorted(job.get("_output_hashes", [])))

    # Jobs with no I/O cannot be identified as re-runs based on artifacts
    if not inputs and not outputs:
        return f"unique:{job.get('job_uid', job.get('id'))}"

    return f"{inputs}|{outputs}"


def _extract_primary_digest(item: dict) -> str | None:
    """Backward-compatible wrapper around shared digest extraction."""
    return extract_primary_digest(item)


class LineageCollector:
    """
    Service for collecting lineage data for artifact upload.

    Collects all jobs and artifacts in the lineage of the target
    artifacts, including:
    - Direct producer jobs
    - Build jobs from the active pipeline
    - All intermediate artifacts

    The collector also deduplicates re-runs, keeping only the latest
    job per unique (inputs, outputs) signature.

    Usage:
        collector = LineageCollector()
        lineage = collector.collect(["hash1", "hash2"], roar_dir)
        # lineage.jobs and lineage.artifacts now hold the full registration payload
    """

    def collect(
        self,
        artifact_hashes: list[str],
        roar_dir: Path,
    ) -> LineageData:
        """
        Collect lineage data for the given artifact hashes.

        Args:
            artifact_hashes: List of artifact hashes to trace lineage for
            roar_dir: Path to .roar directory

        Returns:
            LineageData containing jobs and artifacts in the lineage
        """
        with create_database_context(roar_dir) as ctx_db:
            # Get lineage jobs (with input/output hashes populated)
            lineage_jobs = ctx_db.lineage.get_lineage_jobs(artifact_hashes)

            # Collect artifact hashes from the lineage sub-DAG
            lineage_artifact_hashes = set(artifact_hashes)
            for job in lineage_jobs:
                lineage_artifact_hashes.update(job.get("_input_hashes", []))
                lineage_artifact_hashes.update(job.get("_output_hashes", []))

            # Include build jobs from the active pipeline (filtered to sub-DAG)
            pipeline = ctx_db.sessions.get_active()
            if pipeline:
                lineage_jobs = self._add_build_jobs(
                    ctx_db, pipeline, lineage_jobs, lineage_artifact_hashes
                )

            # Include Ray task jobs linked by parent_job_uid even when the
            # driver doesn't directly read each task output artifact.
            lineage_jobs = self._add_parent_linked_ray_tasks(ctx_db, lineage_jobs)

            # Deduplicate re-runs
            lineage_jobs = self._deduplicate_reruns(lineage_jobs)

            # Collect all artifact hashes referenced by jobs (after deduplication)
            all_lineage_hashes = self._collect_all_hashes(lineage_jobs)

            # Get artifact info for all lineage hashes
            lineage_artifacts = self._get_artifact_info(ctx_db, all_lineage_hashes)

        return LineageData(
            jobs=lineage_jobs,
            artifacts=lineage_artifacts,
            artifact_hashes=all_lineage_hashes,
            pipeline=pipeline,
        )

    def _add_build_jobs(
        self,
        ctx_db,
        pipeline: dict,
        lineage_jobs: list[dict],
        lineage_artifact_hashes: set[str],
    ) -> list[dict]:
        """Add build jobs from the active pipeline that are connected to the lineage."""
        build_jobs = ctx_db.jobs.get_latest_build_jobs(pipeline["id"])

        # Include ALL build jobs from the session - they set up the environment
        build_job_ids = set()
        build_job_list = []

        for bj in build_jobs:
            job_dict = dict(bj)
            job_id = bj["id"]
            inputs = ctx_db.jobs.get_inputs(job_id)
            outputs = ctx_db.jobs.get_outputs(job_id)

            job_dict["_input_hashes"] = [
                h for h in (_extract_primary_digest(inp) for inp in inputs) if h
            ]
            job_dict["_output_hashes"] = [
                h for h in (_extract_primary_digest(out) for out in outputs) if h
            ]

            # Structured inputs/outputs with hash, path, and byte_ranges
            job_dict["_inputs"] = [
                {
                    "hash": h,
                    "path": inp.get("path") or inp.get("first_seen_path", ""),
                    "byte_ranges": inp.get("byte_ranges"),
                }
                for inp in inputs
                if (h := _extract_primary_digest(inp))
            ]
            job_dict["_outputs"] = [
                {
                    "hash": h,
                    "path": out.get("path") or out.get("first_seen_path", ""),
                    "byte_ranges": out.get("byte_ranges"),
                }
                for out in outputs
                if (h := _extract_primary_digest(out))
            ]

            build_job_ids.add(job_id)
            build_job_list.append(job_dict)

        # Combine build jobs with lineage jobs, avoiding duplicates
        return build_job_list + [j for j in lineage_jobs if j["id"] not in build_job_ids]

    def _add_parent_linked_ray_tasks(self, ctx_db, lineage_jobs: list[dict]) -> list[dict]:
        """Include Ray task jobs reachable via parent_job_uid edges."""
        if not lineage_jobs:
            return lineage_jobs

        seen_ids = {job["id"] for job in lineage_jobs}
        result = list(lineage_jobs)
        frontier = {str(job["job_uid"]) for job in lineage_jobs if job.get("job_uid")}

        while frontier:
            ray_children = ctx_db.jobs.get_by_parent_uids(list(frontier), job_type="ray_task")
            next_frontier: set[str] = set()

            for child in ray_children:
                child_id = child["id"]
                if child_id in seen_ids:
                    continue

                job_dict = dict(child)
                inputs = ctx_db.jobs.get_inputs(child_id)
                outputs = ctx_db.jobs.get_outputs(child_id)

                job_dict["_input_hashes"] = [
                    h for h in (_extract_primary_digest(inp) for inp in inputs) if h
                ]
                job_dict["_output_hashes"] = [
                    h for h in (_extract_primary_digest(out) for out in outputs) if h
                ]
                job_dict["_inputs"] = [
                    {
                        "hash": h,
                        "path": inp.get("path") or inp.get("first_seen_path", ""),
                        "byte_ranges": inp.get("byte_ranges"),
                    }
                    for inp in inputs
                    if (h := _extract_primary_digest(inp))
                ]
                job_dict["_outputs"] = [
                    {
                        "hash": h,
                        "path": out.get("path") or out.get("first_seen_path", ""),
                        "byte_ranges": out.get("byte_ranges"),
                    }
                    for out in outputs
                    if (h := _extract_primary_digest(out))
                ]

                result.append(job_dict)
                seen_ids.add(child_id)
                child_uid = job_dict.get("job_uid")
                if child_uid:
                    next_frontier.add(str(child_uid))

            frontier = next_frontier

        return result

    def _deduplicate_reruns(self, jobs: list[dict]) -> list[dict]:
        """
        Eliminate re-runs, keeping only the latest job per signature.

        A node X is a re-run of node Y if they have identical inputs and outputs.
        """
        seen_signatures: dict[str, dict] = {}

        for job in jobs:
            sig = compute_io_signature(job)
            existing = seen_signatures.get(sig)
            # Keep the later job (re-run supersedes earlier runs)
            if existing is None or job["timestamp"] > existing["timestamp"]:
                seen_signatures[sig] = job

        return sorted(seen_signatures.values(), key=lambda j: j["timestamp"])

    def _collect_all_hashes(self, jobs: list[dict]) -> set[str]:
        """Collect all artifact hashes referenced by jobs."""
        all_hashes = set()
        for job in jobs:
            for h in job.get("_input_hashes", []):
                all_hashes.add(h)
            for h in job.get("_output_hashes", []):
                all_hashes.add(h)
        return all_hashes

    def _get_artifact_info(self, ctx_db, hashes: set[str]) -> list[dict]:
        """Get artifact info for all lineage hashes."""
        artifacts = []
        for h in hashes:
            # Prefer blake3 lookups, but allow composite digests in lineage links.
            artifact = ctx_db.artifacts.get_by_hash(h, algorithm="blake3")
            if not artifact:
                artifact = ctx_db.artifacts.get_by_hash(h, algorithm="composite-blake3")
            if not artifact:
                artifact = ctx_db.artifacts.get_by_hash(h)
            if artifact:
                artifact["hash"] = h  # Add the hash we looked up
                artifacts.append(artifact)
        return artifacts
