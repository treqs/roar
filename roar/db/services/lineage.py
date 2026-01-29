"""
Default lineage service implementation.

Provides lineage tracing and DAG reconstruction operations.
"""

from typing import Any

from ...core.interfaces.repositories import ArtifactRepository, JobRepository
from ...core.interfaces.services import LineageService


class DefaultLineageService(LineageService):
    """
    Default implementation of lineage service.

    Provides artifact lineage tracing and DAG reconstruction
    for reproducing data pipelines.
    """

    def __init__(
        self,
        artifact_repo: ArtifactRepository,
        job_repo: JobRepository,
    ):
        """
        Initialize lineage service.

        Args:
            artifact_repo: Artifact repository
            job_repo: Job repository
        """
        self._artifact_repo = artifact_repo
        self._job_repo = job_repo

    def get_artifact_lineage(self, artifact_id: str, depth: int = 3) -> dict[str, Any]:
        """
        Get the lineage of an artifact.

        Traces upstream through the job DAG to show what inputs
        and jobs were needed to produce this artifact.

        Args:
            artifact_id: Artifact UUID or hash
            depth: Maximum depth to trace

        Returns:
            Nested dict representing the lineage tree.
        """
        visited: set[str] = set()

        # Try to resolve by hash if not found by ID
        artifact = self._artifact_repo.get(artifact_id)
        if not artifact:
            artifact = self._artifact_repo.get_by_hash(artifact_id)
            if artifact:
                artifact_id = artifact["id"]

        def trace_upstream(art_id: str, current_depth: int) -> dict[str, Any]:
            if current_depth > depth or art_id in visited:
                return {"id": art_id, "truncated": True}

            visited.add(art_id)
            artifact = self._artifact_repo.get(art_id)
            if not artifact:
                return {"id": art_id, "not_found": True}

            # Find the job that produced this artifact
            jobs = self._artifact_repo.get_jobs(art_id)
            produced_by = jobs.get("produced_by", [])
            producer = produced_by[0] if produced_by else None

            result = {
                "id": art_id,
                "size": artifact["size"],
                "first_seen_path": artifact["first_seen_path"],
                "hashes": artifact.get("hashes", []),
            }

            if producer:
                inputs = self._job_repo.get_inputs(producer["id"], self._artifact_repo)
                result["produced_by"] = {
                    "job_id": producer["id"],
                    "command": producer["command"],
                    "timestamp": producer["timestamp"],
                    "inputs": [
                        trace_upstream(inp["artifact_id"], current_depth + 1) for inp in inputs
                    ],
                }

            return result

        return trace_upstream(artifact_id, 0)

    def get_lineage_jobs(
        self, artifact_ids: list[str], max_depth: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get all jobs in the lineage DAG needed to produce the given artifacts.

        Returns a topologically sorted list of jobs that, when executed
        in order, would reproduce the target artifacts.

        Args:
            artifact_ids: Target artifact UUIDs or hashes
            max_depth: Maximum lineage depth to traverse

        Returns:
            List of job dicts with input/output artifact info, sorted by timestamp.
        """
        # Resolve artifact IDs (could be full ID or hash)
        resolved_ids = []
        for aid in artifact_ids:
            artifact = self._artifact_repo.get(aid)
            if artifact:
                resolved_ids.append(aid)
            else:
                artifact = self._artifact_repo.get_by_hash(aid, algorithm="blake3")
                if artifact:
                    resolved_ids.append(artifact["id"])

        visited_jobs: set[int] = set()
        visited_artifacts: set[str] = set()
        jobs: list[dict[str, Any]] = []

        def trace_upstream(artifact_id: str, current_depth: int):
            if current_depth > max_depth or artifact_id in visited_artifacts:
                return
            visited_artifacts.add(artifact_id)

            # Find the job that produced this artifact
            artifact_jobs = self._artifact_repo.get_jobs(artifact_id)
            produced_by = artifact_jobs.get("produced_by", [])
            producer = produced_by[0] if produced_by else None

            if producer and producer["id"] not in visited_jobs:
                visited_jobs.add(producer["id"])
                job_dict = dict(producer)

                # Get inputs and trace upstream
                inputs = self._job_repo.get_inputs(producer["id"], self._artifact_repo)
                job_dict["_input_artifact_ids"] = [inp["artifact_id"] for inp in inputs]
                job_dict["_input_hashes"] = [
                    h for h in (self._get_blake3(inp) for inp in inputs) if h
                ]
                # Structured inputs with hash and path
                job_dict["_inputs"] = [
                    {"hash": h, "path": inp.get("path") or inp.get("first_seen_path", "")}
                    for inp in inputs
                    if (h := self._get_blake3(inp))
                ]

                for inp in inputs:
                    trace_upstream(inp["artifact_id"], current_depth + 1)

                # Get outputs
                outputs = self._job_repo.get_outputs(producer["id"], self._artifact_repo)
                job_dict["_output_artifact_ids"] = [out["artifact_id"] for out in outputs]
                job_dict["_output_hashes"] = [
                    h for h in (self._get_blake3(out) for out in outputs) if h
                ]
                # Structured outputs with hash and path
                job_dict["_outputs"] = [
                    {"hash": h, "path": out.get("path") or out.get("first_seen_path", "")}
                    for out in outputs
                    if (h := self._get_blake3(out))
                ]

                jobs.append(job_dict)

        for artifact_id in resolved_ids:
            trace_upstream(artifact_id, 0)

        # Sort by timestamp (topological order for DAG)
        jobs.sort(key=lambda j: j["timestamp"])
        return jobs

    def get_filtered_lineage(
        self, artifact_id: str, max_depth: int = 10
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], set[str]]:
        """
        Get filtered lineage for an artifact, including only on-path artifacts.

        Traces upstream through the job DAG and filters outputs to only include
        artifacts that are on the dependency path to the target artifact.

        Args:
            artifact_id: Artifact UUID or hash prefix
            max_depth: Maximum lineage depth to traverse

        Returns:
            Tuple of:
            - target_artifact: Dict with artifact info, or None if not found
            - jobs: List of job dicts with filtered inputs/outputs
            - on_path_hashes: Set of BLAKE3 hashes for artifacts on the path
        """
        # Resolve artifact by ID or hash
        artifact = self._artifact_repo.get(artifact_id)
        if not artifact:
            artifact = self._artifact_repo.get_by_hash(artifact_id, algorithm="blake3")
            if artifact:
                artifact_id = artifact["id"]
            else:
                return None, [], set()

        target_hash = self._get_blake3(artifact)
        if not target_hash:
            return None, [], set()

        on_path_hashes: set[str] = {target_hash}
        visited_artifacts: set[str] = set()
        visited_jobs: set[int] = set()
        jobs: list[dict[str, Any]] = []

        def trace_upstream(art_id: str, current_depth: int) -> None:
            if current_depth > max_depth or art_id in visited_artifacts:
                return
            visited_artifacts.add(art_id)

            # Find the job that produced this artifact
            artifact_jobs = self._artifact_repo.get_jobs(art_id)
            produced_by = artifact_jobs.get("produced_by", [])
            producer = produced_by[0] if produced_by else None

            if producer and producer["id"] not in visited_jobs:
                visited_jobs.add(producer["id"])
                job_dict = dict(producer)

                # Get inputs and add ALL of them to on-path set
                inputs = self._job_repo.get_inputs(producer["id"], self._artifact_repo)
                job_dict["_all_inputs"] = inputs

                for inp in inputs:
                    inp_hash = self._get_blake3(inp)
                    if inp_hash:
                        on_path_hashes.add(inp_hash)
                    # Recursively trace upstream
                    trace_upstream(inp["artifact_id"], current_depth + 1)

                # Get all outputs for later filtering
                outputs = self._job_repo.get_outputs(producer["id"], self._artifact_repo)
                job_dict["_all_outputs"] = outputs

                jobs.append(job_dict)

        trace_upstream(artifact_id, 0)

        # Filter inputs and outputs to only on-path artifacts
        for job in jobs:
            job["_inputs"] = []
            for inp in job.get("_all_inputs", []):
                inp_hash = self._get_blake3(inp)
                if inp_hash and inp_hash in on_path_hashes:
                    job["_inputs"].append(
                        {
                            "hash": inp_hash,
                            "path": inp.get("path") or inp.get("first_seen_path", ""),
                            "size": inp.get("size", 0),
                        }
                    )

            job["_outputs"] = []
            for out in job.get("_all_outputs", []):
                out_hash = self._get_blake3(out)
                if out_hash and out_hash in on_path_hashes:
                    job["_outputs"].append(
                        {
                            "hash": out_hash,
                            "path": out.get("path") or out.get("first_seen_path", ""),
                            "size": out.get("size", 0),
                        }
                    )

            # Clean up temporary fields
            del job["_all_inputs"]
            del job["_all_outputs"]

        # Sort by timestamp (topological order)
        jobs.sort(key=lambda j: j["timestamp"])

        return artifact, jobs, on_path_hashes

    @staticmethod
    def _get_blake3(item: dict[str, Any]) -> str | None:
        """
        Extract BLAKE3 hash from an artifact item.

        Args:
            item: Dict with 'hashes' list

        Returns:
            BLAKE3 digest or None.
        """
        for h in item.get("hashes", []):
            if h.get("algorithm") == "blake3":
                return h.get("digest")
        return None
