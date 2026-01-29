"""
DAG reference resolver service.

Handles resolution of @N and @BN references to actual commands
for DAG step replay functionality.
"""

import re

from ...core.interfaces.logger import ILogger
from ...core.interfaces.run import ResolvedStep


class DAGReferenceResolver:
    """
    Resolves @N and @BN references to actual commands.

    Follows SRP: only handles DAG reference resolution.
    Follows DIP: depends on repository abstractions.
    """

    def __init__(
        self,
        session_repo,
        jobs_repo,
        artifacts_repo,
        lineage_service,
        session_service,
        logger: ILogger | None = None,
    ):
        """
        Initialize resolver with required repositories and services.

        Args:
            session_repo: Session repository for step lookups
            jobs_repo: Job repository for input lookups
            artifacts_repo: Artifact repository for artifact lookups
            lineage_service: Lineage service for tracing producers
            session_service: Session service for stale step detection
            logger: Logger for internal diagnostics
        """
        self._sessions = session_repo
        self._jobs = jobs_repo
        self._artifacts = artifacts_repo
        self._lineage = lineage_service
        self._session_service = session_service
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.container import get_container
            from ...services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def resolve(
        self,
        reference: str,
        param_overrides: dict[str, str],
    ) -> tuple[ResolvedStep | None, str | None]:
        """
        Resolve @N or @BN reference to command.

        Args:
            reference: DAG reference (e.g., "@2" or "@B1")
            param_overrides: Parameter overrides to apply to command

        Returns:
            Tuple of (ResolvedStep, None) on success, or (None, error_message) on failure
        """
        self.logger.debug(
            "Resolving DAG reference: %s with overrides=%s", reference, param_overrides
        )

        # Parse reference
        step_ref = reference[1:]  # Remove @
        is_build = step_ref.upper().startswith("B")
        if is_build:
            step_ref = step_ref[1:]

        if not step_ref.isdigit():
            self.logger.debug("Invalid reference format: %s", reference)
            return None, f"Invalid DAG reference '{reference}'. Use @N or @BN where N is a number."

        step_num = int(step_ref)
        self.logger.debug("Parsed reference: step_num=%d, is_build=%s", step_num, is_build)

        # Look up the step
        session = self._sessions.get_active()
        if not session:
            self.logger.debug("No active session found")
            return None, "No active DAG."

        self.logger.debug("Looking up step in session: session_id=%d", session["id"])
        job_type = "build" if is_build else None
        step = self._sessions.get_step_by_number(session["id"], step_num, job_type=job_type)
        if not step:
            self.logger.debug("Step not found: step_num=%d, job_type=%s", step_num, job_type)
            prefix = "@B" if is_build else "@"
            return None, f"No node {prefix}{step_num} in DAG."

        self.logger.debug(
            "Found step: id=%d, command=%s",
            step["id"],
            step["command"][:50] if step.get("command") else None,
        )

        # Check for stale upstream steps
        stale_steps = set(self._session_service.get_stale_steps(session["id"]))
        self.logger.debug("Stale steps in session: %s", stale_steps)
        stale_upstream = self._find_stale_upstream(step, stale_steps, session["id"])
        self.logger.debug("Stale upstream steps: %s", stale_upstream)

        # Get the command and apply overrides
        command = self._apply_overrides(step["command"], param_overrides)
        self.logger.debug("Final command after overrides: %s", command)

        return ResolvedStep(
            step_number=step_num,
            command=command,
            is_build=is_build,
            original_step=step,
            stale_upstream=stale_upstream,
        ), None

    def _find_stale_upstream(
        self,
        step: dict,
        stale_steps: set,
        session_id: int,
    ) -> list[int]:
        """Find which upstream steps are stale."""
        step_num = step.get("step_number")
        if step_num not in stale_steps:
            return []

        upstream_stale = []
        job_inputs = self._jobs.get_inputs(step["id"], self._artifacts)

        for inp in job_inputs:
            artifact_hash = inp.get("artifact_hash")
            if not artifact_hash:
                continue

            producer_jobs = self._lineage.get_artifact_jobs(artifact_hash)
            for pj in producer_jobs.get("produced_by", []):
                producer_step = self._sessions.get_step_for_job(session_id, pj["id"])
                if (
                    producer_step
                    and producer_step["step_number"] in stale_steps
                    and producer_step["step_number"] not in upstream_stale
                ):
                    upstream_stale.append(producer_step["step_number"])

        upstream_stale.sort()
        return upstream_stale

    def _apply_overrides(self, command: str, overrides: dict[str, str]) -> str:
        """Apply parameter overrides to a command string."""
        if not overrides:
            return command

        for key, value in overrides.items():
            if f"--{key}" in command or f"--{key}=" in command:
                pattern = rf"--{re.escape(key)}[=\s]+\S+"
                command = re.sub(pattern, f"--{key}={value}", command)
            else:
                command = f"{command} --{key}={value}"

        return command
