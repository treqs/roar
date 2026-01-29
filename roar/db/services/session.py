"""
Default session service implementation.

Provides session analysis operations like staleness detection
and dependency tracking.
"""

from typing import Any

from ...core.interfaces.repositories import ArtifactRepository, JobRepository, SessionRepository
from ...core.interfaces.services import SessionService


class DefaultSessionService(SessionService):
    """
    Default implementation of session service.

    Provides staleness detection, downstream analysis, and other
    session-level operations.
    """

    def __init__(
        self,
        session_repo: SessionRepository,
        job_repo: JobRepository,
        artifact_repo: ArtifactRepository,
    ):
        """
        Initialize session service.

        Args:
            session_repo: Session repository
            job_repo: Job repository
            artifact_repo: Artifact repository
        """
        self._session_repo = session_repo
        self._job_repo = job_repo
        self._artifact_repo = artifact_repo

    def get_stale_steps(self, session_id: int) -> list[int]:
        """
        Find steps that are stale (consumed outputs that have since changed).

        A step is stale if:
        1. It consumed an artifact that was later re-produced with a different hash
        2. It depends on another step that is stale

        Args:
            session_id: Session ID

        Returns:
            List of stale step numbers.
        """
        steps = self._session_repo.get_steps(session_id)
        if not steps:
            return []

        # Get latest run of each step
        latest_by_step: dict[int, dict[str, Any]] = {}
        for step in steps:
            num = step["step_number"]
            if num not in latest_by_step or step["timestamp"] > latest_by_step[num]["timestamp"]:
                latest_by_step[num] = step

        # Map output paths to their current artifact IDs
        output_path_to_current: dict[str, tuple] = {}
        for num, step in latest_by_step.items():
            outputs = self._job_repo.get_outputs(step["id"], self._artifact_repo)
            for out in outputs:
                path = out.get("path") or out.get("first_seen_path")
                if not path:
                    continue  # Skip entries without valid paths
                output_path_to_current[path] = (num, out["artifact_id"])

        # Build dependency graph and track consumed artifacts
        depends_on: dict[int, set[int]] = {}
        consumed_artifacts: dict[int, dict[str, str]] = {}

        for num, step in latest_by_step.items():
            depends_on[num] = set()
            consumed_artifacts[num] = {}
            inputs = self._job_repo.get_inputs(step["id"], self._artifact_repo)

            for inp in inputs:
                path = inp.get("path") or inp.get("first_seen_path")
                if not path:
                    continue  # Skip entries without valid paths
                artifact_id = inp["artifact_id"]

                if path in output_path_to_current:
                    producer_step, _ = output_path_to_current[path]
                    if producer_step != num and producer_step < num:
                        depends_on[num].add(producer_step)
                        consumed_artifacts[num][path] = artifact_id

        # Find directly stale steps (consumed different artifact than current)
        directly_stale: set[int] = set()
        for num, _step in latest_by_step.items():
            for path, consumed_artifact_id in consumed_artifacts[num].items():
                if path in output_path_to_current:
                    _, current_artifact_id = output_path_to_current[path]
                    if consumed_artifact_id != current_artifact_id:
                        directly_stale.add(num)
                        break

        # Propagate staleness to downstream steps
        stale_steps = set(directly_stale)
        changed = True
        while changed:
            changed = False
            for num in latest_by_step:
                if num not in stale_steps and depends_on[num] & stale_steps:
                    stale_steps.add(num)
                    changed = True

        return list(stale_steps)

    def get_stale_artifacts(self, session_id: int) -> list[str]:
        """
        Return artifact IDs that are stale (produced by stale steps).

        An artifact is stale if its producer step is stale.

        Args:
            session_id: Session ID

        Returns:
            List of stale artifact IDs.
        """
        stale_steps = set(self.get_stale_steps(session_id))
        if not stale_steps:
            return []

        steps = self._session_repo.get_steps(session_id)
        if not steps:
            return []

        # Get latest run of each stale step
        latest_by_step: dict[int, dict[str, Any]] = {}
        for step in steps:
            num = step["step_number"]
            if num in stale_steps and (
                num not in latest_by_step or step["timestamp"] > latest_by_step[num]["timestamp"]
            ):
                latest_by_step[num] = step

        # Collect artifact IDs from stale steps
        stale_artifact_ids: list[str] = []
        for _num, step in latest_by_step.items():
            outputs = self._job_repo.get_outputs(step["id"], self._artifact_repo)
            for out in outputs:
                artifact_id = out.get("artifact_id")
                if artifact_id:
                    stale_artifact_ids.append(str(artifact_id))

        return stale_artifact_ids

    def get_downstream_steps(self, session_id: int, step_number: int) -> list[int]:
        """
        Find all steps downstream of the given step.

        A step is downstream if it consumes any output artifact
        produced by the given step.

        Args:
            session_id: Session ID
            step_number: Step number to find downstream of

        Returns:
            Sorted list of downstream step numbers.
        """
        steps = self._session_repo.get_steps(session_id)
        if not steps:
            return []

        # Get latest run of each step
        latest_by_step: dict[int, dict[str, Any]] = {}
        for step in steps:
            num = step["step_number"]
            if num not in latest_by_step or step["timestamp"] > latest_by_step[num]["timestamp"]:
                latest_by_step[num] = step

        if step_number not in latest_by_step:
            return []

        # Get output artifact IDs from source step
        source_step = latest_by_step[step_number]
        source_outputs = self._job_repo.get_outputs(source_step["id"], self._artifact_repo)
        source_artifact_ids = {out["artifact_id"] for out in source_outputs}

        if not source_artifact_ids:
            return []

        # Find steps that consume any of these artifacts
        downstream = []
        for num, step in latest_by_step.items():
            if num == step_number:
                continue
            inputs = self._job_repo.get_inputs(step["id"], self._artifact_repo)
            input_artifact_ids = {inp["artifact_id"] for inp in inputs}
            if source_artifact_ids & input_artifact_ids:
                downstream.append(num)

        return sorted(downstream)

    def compute_step_identity(
        self,
        input_paths: list[str],
        output_paths: list[str],
        repo_root: str | None = None,
        command: str | None = None,
    ) -> str:
        """
        Compute step identity hash from normalized paths.

        Delegates to session repository for the actual computation.

        Args:
            input_paths: List of input file paths
            output_paths: List of output file paths
            repo_root: Repository root for path normalization
            command: Command string (used if no inputs/outputs)

        Returns:
            Step identity hash.
        """
        return self._session_repo.compute_step_identity(
            input_paths, output_paths, repo_root, command
        )

    def get_summary(self, session_id: int) -> dict[str, Any]:
        """
        Get a summary of a session for display.

        Args:
            session_id: Session ID

        Returns:
            Summary dict with id, hash, steps, staleness info, etc.
        """
        result = self._session_repo.get_summary(session_id, self._job_repo)
        return result if result is not None else {}

    def check_git_consistency(self, session_id: int) -> dict[str, Any]:
        """
        Check if a session has mixed git commits.

        Args:
            session_id: Session ID

        Returns:
            Dict with consistent (bool), commits (list), warning (str or None).
        """
        return self._session_repo.check_git_consistency(session_id)
