"""
Shared publish lineage collection and enrichment.

This module is the canonical home for publish-time lineage traversal.
"""

from __future__ import annotations

from pathlib import Path

from ...core.digests import extract_primary_digest
from ...core.interfaces.lineage import LineageData
from ...db.context import create_database_context
from ...db.query_context import create_query_database_context
from ...execution.framework.registry import is_execution_task_job


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
    Collect lineage data for publish and registration flows.

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
            artifact_hashes: List of artifact hashes to trace lineage for.
            roar_dir: Path to `.roar`.

        Returns:
            LineageData containing jobs and artifacts in the lineage.
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

            # Include parent jobs for any lineage tasks so parent_job_uid
            # references can be registered safely in GLaaS.
            lineage_jobs = self._add_parent_jobs(ctx_db, lineage_jobs)

            # Include distributed child jobs linked by parent_job_uid even when the
            # root job doesn't directly read each child output artifact.
            lineage_jobs = self._add_parent_linked_execution_tasks(ctx_db, lineage_jobs)

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

    def collect_step(
        self,
        session_id: int,
        step_number: int,
        roar_dir: Path,
        job_type: str | None = None,
    ) -> LineageData:
        """Collect lineage for a visible DAG step within a session."""
        with create_database_context(roar_dir) as ctx_db:
            session = ctx_db.sessions.get(session_id)
            if not session:
                return LineageData()

            step_jobs = self._get_step_jobs(ctx_db, session_id, step_number, job_type=job_type)
            if not step_jobs:
                return LineageData(pipeline=session)

            hydrated_step_jobs = [self._hydrate_job(ctx_db, job) for job in step_jobs]
            target_hashes = sorted(
                {
                    digest
                    for job in hydrated_step_jobs
                    for digest in job.get("_output_hashes", [])
                    if digest
                }
            )
            if not target_hashes:
                target_hashes = sorted(
                    {
                        digest
                        for job in hydrated_step_jobs
                        for digest in job.get("_input_hashes", [])
                        if digest
                    }
                )

            if target_hashes:
                lineage_jobs = ctx_db.lineage.get_lineage_jobs(target_hashes)
                if session:
                    lineage_jobs = self._add_build_jobs(
                        ctx_db, session, lineage_jobs, set(target_hashes)
                    )
                lineage_jobs = self._add_parent_jobs(ctx_db, lineage_jobs)
                lineage_jobs = self._add_parent_linked_execution_tasks(ctx_db, lineage_jobs)
            else:
                lineage_jobs = []

            seen_ids = {job["id"] for job in lineage_jobs}
            for job in hydrated_step_jobs:
                if job["id"] not in seen_ids:
                    lineage_jobs.append(job)
                    seen_ids.add(job["id"])

            lineage_jobs.sort(key=lambda job: job["timestamp"])
            all_hashes = self._collect_all_hashes(lineage_jobs)
            artifacts = self._get_artifact_info(ctx_db, all_hashes)

        return LineageData(
            jobs=lineage_jobs,
            artifacts=artifacts,
            artifact_hashes=all_hashes,
            pipeline=session,
        )

    def collect_step_read_only(
        self,
        session_id: int,
        step_number: int,
        roar_dir: Path,
        job_type: str | None = None,
    ) -> LineageData:
        """Collect step lineage through the sqlite query context for preview flows."""
        with create_query_database_context(roar_dir) as ctx_db:
            session = ctx_db.sessions.get(session_id)
            if not session:
                return LineageData()

            step_jobs = self._get_step_jobs(ctx_db, session_id, step_number, job_type=job_type)
            if not step_jobs:
                return LineageData(pipeline=session)

            hydrated_step_jobs = [self._hydrate_job(ctx_db, job) for job in step_jobs]
            target_hashes = sorted(
                {
                    digest
                    for job in hydrated_step_jobs
                    for digest in job.get("_output_hashes", [])
                    if digest
                }
            )
            if not target_hashes:
                target_hashes = sorted(
                    {
                        digest
                        for job in hydrated_step_jobs
                        for digest in job.get("_input_hashes", [])
                        if digest
                    }
                )

            if target_hashes:
                lineage_jobs = self._get_lineage_jobs_read_only(ctx_db, target_hashes)
                if session:
                    lineage_jobs = self._add_build_jobs(
                        ctx_db, session, lineage_jobs, set(target_hashes)
                    )
                lineage_jobs = self._add_parent_jobs(ctx_db, lineage_jobs)
                lineage_jobs = self._add_parent_linked_execution_tasks(ctx_db, lineage_jobs)
            else:
                lineage_jobs = []

            seen_ids = {job["id"] for job in lineage_jobs}
            for job in hydrated_step_jobs:
                if job["id"] not in seen_ids:
                    lineage_jobs.append(job)
                    seen_ids.add(job["id"])

            lineage_jobs.sort(key=lambda job: job["timestamp"])
            all_hashes = self._collect_all_hashes(lineage_jobs)
            artifacts = self._get_artifact_info(ctx_db, all_hashes)

        return LineageData(
            jobs=lineage_jobs,
            artifacts=artifacts,
            artifact_hashes=all_hashes,
            pipeline=session,
        )

    def collect_session(
        self,
        session_id: int,
        roar_dir: Path,
    ) -> LineageData:
        """Collect all jobs and artifacts recorded in a local session."""
        with create_database_context(roar_dir) as ctx_db:
            session = ctx_db.sessions.get(session_id)
            if not session:
                return LineageData()

            jobs = [self._hydrate_job(ctx_db, job) for job in ctx_db.sessions.get_steps(session_id)]
            jobs = self._add_parent_jobs(ctx_db, jobs)
            jobs = self._add_parent_linked_execution_tasks(ctx_db, jobs)
            jobs.sort(key=lambda job: job["timestamp"])
            all_hashes = self._collect_all_hashes(jobs)
            artifacts = self._get_artifact_info(ctx_db, all_hashes)

        return LineageData(
            jobs=jobs,
            artifacts=artifacts,
            artifact_hashes=all_hashes,
            pipeline=session,
        )

    def collect_job(
        self,
        job_uid: str,
        roar_dir: Path,
    ) -> LineageData:
        """Collect lineage for a single job identified by local job UID or prefix."""
        with create_database_context(roar_dir) as ctx_db:
            job = ctx_db.jobs.get_by_uid(job_uid)
            if not job:
                return LineageData()

            session_id = int(job.get("session_id") or 0)
            session = ctx_db.sessions.get(session_id) if session_id else None

            hydrated_job = self._hydrate_job(ctx_db, job)
            target_hashes = sorted(
                {
                    digest
                    for digest in hydrated_job.get("_output_hashes", [])
                    if isinstance(digest, str) and digest
                }
            )
            if not target_hashes:
                target_hashes = sorted(
                    {
                        digest
                        for digest in hydrated_job.get("_input_hashes", [])
                        if isinstance(digest, str) and digest
                    }
                )

            if target_hashes:
                lineage_jobs = ctx_db.lineage.get_lineage_jobs(target_hashes)
                if session:
                    lineage_jobs = self._add_build_jobs(
                        ctx_db, session, lineage_jobs, set(target_hashes)
                    )
                lineage_jobs = self._add_parent_jobs(ctx_db, lineage_jobs)
                lineage_jobs = self._add_parent_linked_execution_tasks(ctx_db, lineage_jobs)
            else:
                lineage_jobs = []

            seen_ids = {job["id"] for job in lineage_jobs}
            if hydrated_job["id"] not in seen_ids:
                lineage_jobs.append(hydrated_job)

            lineage_jobs.sort(key=lambda candidate: candidate["timestamp"])
            all_hashes = self._collect_all_hashes(lineage_jobs)
            artifacts = self._get_artifact_info(ctx_db, all_hashes)

        return LineageData(
            jobs=lineage_jobs,
            artifacts=artifacts,
            artifact_hashes=all_hashes,
            pipeline=session,
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

    def _get_lineage_jobs_read_only(
        self,
        ctx_db,
        artifact_ids: list[str],
        max_depth: int = 10,
    ) -> list[dict]:
        """Reconstruct lineage jobs without loading the SQLAlchemy service stack."""
        resolved_ids = []
        for artifact_id in artifact_ids:
            artifact = ctx_db.artifacts.get(artifact_id)
            if artifact:
                resolved_ids.append(artifact_id)
                continue
            artifact = ctx_db.artifacts.get_by_hash(artifact_id)
            if artifact:
                resolved_ids.append(artifact["id"])

        visited_jobs: set[int] = set()
        visited_artifacts: set[str] = set()
        jobs: list[dict] = []

        def trace_upstream(artifact_id: str, current_depth: int) -> None:
            if current_depth > max_depth or artifact_id in visited_artifacts:
                return
            visited_artifacts.add(artifact_id)

            artifact_jobs = ctx_db.artifacts.get_jobs(artifact_id)
            produced_by = artifact_jobs.get("produced_by", [])
            producer = produced_by[0] if produced_by else None

            if producer and producer["id"] not in visited_jobs:
                visited_jobs.add(producer["id"])
                job_dict = dict(producer)

                inputs = ctx_db.jobs.get_inputs(producer["id"])
                job_dict["_input_artifact_ids"] = [inp["artifact_id"] for inp in inputs]
                job_dict["_input_hashes"] = [
                    h for h in (_extract_primary_digest(inp) for inp in inputs) if h
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

                for inp in inputs:
                    trace_upstream(inp["artifact_id"], current_depth + 1)

                outputs = ctx_db.jobs.get_outputs(producer["id"])
                job_dict["_output_artifact_ids"] = [out["artifact_id"] for out in outputs]
                job_dict["_output_hashes"] = [
                    h for h in (_extract_primary_digest(out) for out in outputs) if h
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

                jobs.append(job_dict)

        for artifact_id in resolved_ids:
            trace_upstream(artifact_id, 0)

        jobs.sort(key=lambda job: job["timestamp"])
        return jobs

    def _add_parent_linked_execution_tasks(self, ctx_db, lineage_jobs: list[dict]) -> list[dict]:
        """Include distributed child jobs reachable via parent_job_uid edges."""
        if not lineage_jobs:
            return lineage_jobs

        seen_ids = {job["id"] for job in lineage_jobs}
        result = list(lineage_jobs)
        frontier = {str(job["job_uid"]) for job in lineage_jobs if job.get("job_uid")}

        while frontier:
            child_jobs = ctx_db.jobs.get_by_parent_uids(list(frontier))
            next_frontier: set[str] = set()

            for child in child_jobs:
                if not is_execution_task_job(child):
                    continue
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

    def _add_parent_jobs(self, ctx_db, lineage_jobs: list[dict]) -> list[dict]:
        """Include parent jobs referenced by ``parent_job_uid`` recursively."""
        if not lineage_jobs:
            return lineage_jobs

        seen_ids = {job["id"] for job in lineage_jobs}
        result = list(lineage_jobs)
        frontier = {
            str(job["parent_job_uid"])
            for job in lineage_jobs
            if isinstance(job.get("parent_job_uid"), str) and job["parent_job_uid"]
        }

        while frontier:
            next_frontier: set[str] = set()
            for parent_uid in frontier:
                parent = ctx_db.jobs.get_by_uid(parent_uid)
                if not parent:
                    continue

                parent_id = parent["id"]
                if parent_id in seen_ids:
                    continue

                job_dict = dict(parent)
                inputs = ctx_db.jobs.get_inputs(parent_id)
                outputs = ctx_db.jobs.get_outputs(parent_id)

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
                seen_ids.add(parent_id)

                grand_parent_uid = job_dict.get("parent_job_uid")
                if isinstance(grand_parent_uid, str) and grand_parent_uid:
                    next_frontier.add(grand_parent_uid)

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

    def _get_step_jobs(
        self,
        ctx_db,
        session_id: int,
        step_number: int,
        job_type: str | None = None,
    ) -> list[dict]:
        jobs = []
        for job in ctx_db.sessions.get_steps(session_id):
            if int(job.get("step_number") or 0) != step_number:
                continue
            normalized_job_type = job.get("job_type")
            if job_type == "build":
                if normalized_job_type != "build":
                    continue
            elif normalized_job_type == "build":
                continue
            jobs.append(job)
        return jobs

    def _hydrate_job(self, ctx_db, job: dict) -> dict:
        job_dict = dict(job)
        job_id = job_dict["id"]
        inputs = ctx_db.jobs.get_inputs(job_id)
        outputs = ctx_db.jobs.get_outputs(job_id)

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
        return job_dict
