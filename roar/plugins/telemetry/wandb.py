"""
Weights & Biases telemetry provider.

Detects and extracts W&B run information from jobs.
"""

import os
import re

from ...core.interfaces.telemetry import TelemetryRunInfo
from .base import BaseTelemetryProvider

# Cache for wandb run URLs to avoid repeated API calls
_wandb_url_cache: dict = {}


class WandBTelemetryProvider(BaseTelemetryProvider):
    """
    Weights & Biases telemetry provider.

    Detects W&B runs created during job execution by scanning
    the wandb directory for run logs.
    """

    @property
    def name(self) -> str:
        return "wandb"

    def is_available(self) -> bool:
        """Check if wandb is installed."""
        import importlib.util

        return importlib.util.find_spec("wandb") is not None

    def detect_runs(
        self,
        repo_root: str,
        start_time: float,
        end_time: float,
        allow_incomplete: bool = False,
    ) -> list[TelemetryRunInfo]:
        """
        Detect W&B runs created during a job's execution.

        Scans the wandb directory for run directories that were created
        during the job's execution window.
        """
        wandb_dir = os.path.join(repo_root, "wandb")
        if not os.path.isdir(wandb_dir):
            return []

        runs = []
        for entry in os.listdir(wandb_dir):
            if not entry.startswith("run-"):
                continue

            run_path = os.path.join(wandb_dir, entry)
            if not os.path.isdir(run_path):
                continue

            try:
                # Use directory creation time as a proxy for run start
                dir_stat = os.stat(run_path)
                dir_ctime = dir_stat.st_ctime

                # Allow some tolerance
                if dir_ctime >= start_time - 5 and dir_ctime <= end_time + 5:
                    run = self._extract_run_info(run_path, allow_incomplete)
                    if run:
                        runs.append(run)
            except (OSError, ValueError):
                continue

        return runs

    def _extract_run_info(
        self,
        run_dir: str,
        allow_incomplete: bool = False,
    ) -> TelemetryRunInfo | None:
        """
        Extract W&B run info from a run directory.

        Args:
            run_dir: Path to the wandb run directory
            allow_incomplete: If True, try to find URL for running jobs

        Returns:
            TelemetryRun object or None if not extractable
        """
        debug_log = os.path.join(run_dir, "logs", "debug.log")
        if not os.path.exists(debug_log):
            return None

        try:
            with open(debug_log) as f:
                content = f.read()

            # Look for 'finishing run entity/project/run_id' (completed runs)
            match = re.search(r"finishing run ([^/\s]+)/([^/\s]+)/([^/\s]+)", content)
            if match:
                entity, project, run_id = match.groups()
                url = f"https://wandb.ai/{entity}/{project}/runs/{run_id}"
                return TelemetryRunInfo(
                    provider=self.name,  # type: ignore[arg-type]
                    run_id=run_id,
                    url=url,
                    project=project,
                    entity=entity,
                )

            # For incomplete runs, try API lookup
            if allow_incomplete:
                dir_name = os.path.basename(run_dir)
                if dir_name.startswith("run-"):
                    parts = dir_name.split("-")
                    if len(parts) >= 3:
                        run_id = parts[-1]
                        api_url = self.get_run_url(run_id)
                        if api_url:
                            return TelemetryRunInfo(
                                provider=self.name,  # type: ignore[arg-type]
                                run_id=run_id,
                                url=api_url,
                            )

        except OSError:
            pass

        return None

    def get_run_url(self, run_id: str) -> str | None:
        """
        Find a wandb run URL by searching across all projects.

        Results are cached to avoid repeated API calls.
        """
        # Check cache first
        if run_id in _wandb_url_cache:
            return _wandb_url_cache[run_id]

        try:
            import wandb

            api = wandb.Api(timeout=10)
            entity = api.default_entity
            if not entity:
                return None

            # Search for the run across all projects
            for project in api.projects(entity):
                try:
                    run = api.run(f"{entity}/{project.name}/{run_id}")
                    url = run.url
                    _wandb_url_cache[run_id] = url
                    return url
                except wandb.errors.CommError:
                    pass  # Run not in this project
                except Exception:
                    pass
        except Exception:
            pass

        return None
