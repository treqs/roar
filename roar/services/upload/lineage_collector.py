"""
Lineage collector service for upload operations.

Extracted from put.py to follow Single Responsibility Principle.
This service collects all lineage data (jobs, artifacts) needed
for registering artifacts with GLaaS.
"""

from pathlib import Path

from sqlalchemy import text

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


def _get_blake3(item: dict) -> str | None:
    """Extract blake3 hash from item's hashes list."""
    for h in item.get("hashes", []):
        if h.get("algorithm") == "blake3":
            return h.get("digest")
    return None


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
        print(f"Jobs: {len(lineage.jobs)}, Artifacts: {len(lineage.artifacts)}")
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
        build_jobs = ctx_db.conn.execute(
            text("""
                SELECT j.* FROM jobs j
                INNER JOIN (
                    SELECT step_number, MAX(id) as max_id
                    FROM jobs
                    WHERE session_id = :session_id AND job_type = 'build'
                    GROUP BY step_number
                ) latest ON j.id = latest.max_id
                ORDER BY j.step_number
            """),
            {"session_id": pipeline["id"]},
        ).fetchall()

        # Include ALL build jobs from the session - they set up the environment
        build_job_ids = set()
        build_job_list = []

        for bj in build_jobs:
            job_dict = dict(bj._mapping) if hasattr(bj, "_mapping") else dict(bj)
            job_id = bj.id if hasattr(bj, "id") else bj["id"]
            inputs = ctx_db.jobs.get_inputs(job_id, ctx_db.artifacts)
            outputs = ctx_db.jobs.get_outputs(job_id, ctx_db.artifacts)

            job_dict["_input_hashes"] = [h for h in (_get_blake3(inp) for inp in inputs) if h]
            job_dict["_output_hashes"] = [h for h in (_get_blake3(out) for out in outputs) if h]

            # Structured inputs/outputs with hash and path
            job_dict["_inputs"] = [
                {"hash": h, "path": inp.get("path") or inp.get("first_seen_path", "")}
                for inp in inputs
                if (h := _get_blake3(inp))
            ]
            job_dict["_outputs"] = [
                {"hash": h, "path": out.get("path") or out.get("first_seen_path", "")}
                for out in outputs
                if (h := _get_blake3(out))
            ]

            build_job_ids.add(job_id)
            build_job_list.append(job_dict)

        # Combine build jobs with lineage jobs, avoiding duplicates
        return build_job_list + [j for j in lineage_jobs if j["id"] not in build_job_ids]

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
            artifact = ctx_db.artifacts.get_by_hash(h, algorithm="blake3")
            if artifact:
                artifact["hash"] = h  # Add the hash we looked up
                artifacts.append(artifact)
        return artifacts
