"""Analyzer for experiment tracking services (W&B, MLflow, Neptune)."""

import json
import re
from pathlib import Path
from typing import Any, ClassVar

from . import register
from .base import Analyzer


@register
class ExperimentTrackerAnalyzer(Analyzer):
    name = "experiment_tracking"
    description = "Detect experiment tracker usage and extract run URLs"

    # Patterns that indicate tracker usage in written files
    TRACKER_PATTERNS: ClassVar[dict[str, list[str]]] = {
        "wandb": ["wandb/", ".wandb"],
        "mlflow": ["mlruns/", "mlartifacts/"],
        "neptune": [".neptune/"],
        "tensorboard": ["/runs/", "events.out.tfevents"],
    }

    # Files to ignore (tracker metadata, not user artifacts)
    IGNORE_PATTERNS: ClassVar[list[str]] = [
        "wandb/*",
        "mlruns/*",
        "mlartifacts/*",
        ".neptune/*",
    ]

    def relevant(self, context: dict) -> bool:
        """Check if any tracker directories were written to."""
        written = context.get("written_files", [])
        for path in written:
            for _tracker, patterns in self.TRACKER_PATTERNS.items():
                if any(p in path for p in patterns):
                    return True
        return False

    def analyze(self, context: dict) -> dict | None:
        written = context.get("written_files", [])
        env = context.get("env", {})

        results: dict[str, Any] = {
            "trackers_detected": [],
            "runs": [],
            "ignore_patterns": [],
        }

        # Detect which trackers were used
        trackers_found = set()
        for path in written:
            for tracker, patterns in self.TRACKER_PATTERNS.items():
                if any(p in path for p in patterns):
                    trackers_found.add(tracker)

        results["trackers_detected"] = sorted(trackers_found)

        # Extract run info for each detected tracker
        for tracker in trackers_found:
            run_info = self._extract_run_info(tracker, written, env)
            if run_info:
                results["runs"].append(run_info)

            # Add ignore patterns for this tracker
            for pattern in self.IGNORE_PATTERNS:
                if (pattern.startswith(tracker) or tracker in pattern) and pattern not in results[
                    "ignore_patterns"
                ]:
                    results["ignore_patterns"].append(pattern)

        # Add standard ignore patterns for detected trackers
        if "wandb" in trackers_found:
            results["ignore_patterns"].extend(["wandb/*", "*.wandb"])
        if "mlflow" in trackers_found:
            results["ignore_patterns"].extend(["mlruns/*", "mlartifacts/*"])
        if "neptune" in trackers_found:
            results["ignore_patterns"].append(".neptune/*")

        # Dedupe
        results["ignore_patterns"] = sorted(set(results["ignore_patterns"]))

        return results if results["trackers_detected"] else None

    def _extract_run_info(self, tracker: str, written_files: list, env: dict) -> dict | None:
        """Extract run URL and metadata for a specific tracker."""
        if tracker == "wandb":
            return self._extract_wandb_info(written_files, env)
        elif tracker == "mlflow":
            return self._extract_mlflow_info(written_files, env)
        elif tracker == "neptune":
            return self._extract_neptune_info(written_files, env)
        return None

    def _extract_wandb_info(self, written_files: list, env: dict) -> dict | None:
        """Extract W&B run info from local files."""
        info = {"tracker": "wandb"}

        # Find wandb run directories
        wandb_dirs = set()
        for path in written_files:
            if "wandb/" in path:
                # Extract the wandb directory path
                idx = path.find("wandb/")
                base = path[: idx + 6]  # Include "wandb/"
                wandb_dirs.add(base)

        # Look for run metadata in wandb directories
        for wandb_dir in wandb_dirs:
            run_dir: Path | None = None
            # Check for latest-run symlink or run directories
            latest_run = Path(wandb_dir) / "latest-run"
            if latest_run.exists() and latest_run.is_symlink():
                run_dir = latest_run.resolve()
            else:
                # Find most recent run-* directory
                wandb_path = Path(wandb_dir)
                if wandb_path.exists():
                    run_dirs = sorted(
                        wandb_path.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True
                    )
                    run_dir = run_dirs[0] if run_dirs else None
                else:
                    run_dir = None

            if run_dir and run_dir.exists():
                # Try to read run metadata
                run_metadata = run_dir / "files" / "wandb-metadata.json"
                if run_metadata.exists():
                    try:
                        with open(run_metadata) as f:
                            metadata = json.load(f)
                        info["run_id"] = metadata.get("run_id")
                        info["project"] = metadata.get("project")
                        info["entity"] = metadata.get("entity")
                        if all(k in info for k in ["entity", "project", "run_id"]):
                            info["url"] = (
                                f"https://wandb.ai/{info['entity']}/{info['project']}/runs/{info['run_id']}"
                            )
                    except (OSError, json.JSONDecodeError):
                        pass

                # Also check wandb-summary.json for run info
                summary_file = run_dir / "files" / "wandb-summary.json"
                if summary_file.exists() and "run_id" not in info:
                    try:
                        with open(summary_file) as f:
                            summary = json.load(f)
                        # Summary might have _wandb key with run info
                        wandb_info = summary.get("_wandb", {})
                        if "runtime" in wandb_info:
                            info["runtime_seconds"] = wandb_info["runtime"]
                    except (OSError, json.JSONDecodeError):
                        pass

        # Fall back to env vars
        if "url" not in info:
            entity = env.get("WANDB_ENTITY", "")
            project = env.get("WANDB_PROJECT", "")
            if entity and project:
                info["entity"] = entity
                info["project"] = project
                # Can't get run_id from env alone

        return info if len(info) > 1 else None

    def _extract_mlflow_info(self, written_files: list, env: dict) -> dict | None:
        """Extract MLflow run info from local files."""
        info = {"tracker": "mlflow"}

        # Get tracking URI from env
        tracking_uri = env.get("MLFLOW_TRACKING_URI", "")
        if tracking_uri:
            info["tracking_uri"] = tracking_uri

        # Find mlruns directories
        for path in written_files:
            if "mlruns/" in path:
                # Parse path structure: mlruns/<experiment_id>/<run_id>/...
                match = re.search(r"mlruns/(\d+)/([a-f0-9]{32})/", path)
                if match:
                    info["experiment_id"] = match.group(1)
                    info["run_id"] = match.group(2)

                    # Try to read run metadata
                    idx = path.find("mlruns/")
                    mlruns_base = path[: idx + 7]
                    meta_path = (
                        Path(mlruns_base) / info["experiment_id"] / info["run_id"] / "meta.yaml"
                    )
                    if meta_path.exists():
                        try:
                            import yaml  # type: ignore[import-untyped]

                            with open(meta_path) as f:
                                meta = yaml.safe_load(f)
                            info["run_name"] = meta.get("run_name")
                            info["status"] = meta.get("status")
                            info["start_time"] = meta.get("start_time")
                        except (OSError, ImportError):
                            pass

                    # Build URL if we have tracking URI
                    if tracking_uri and tracking_uri.startswith("http"):
                        info["url"] = (
                            f"{tracking_uri.rstrip('/')}/#/experiments/{info['experiment_id']}/runs/{info['run_id']}"
                        )
                    break

        return info if len(info) > 1 else None

    def _extract_neptune_info(self, written_files: list, env: dict) -> dict | None:
        """Extract Neptune run info from local files."""
        info = {"tracker": "neptune"}

        # Get project from env
        project = env.get("NEPTUNE_PROJECT", "")
        if project:
            info["project"] = project

        # Find .neptune directories and look for run info
        for path in written_files:
            if ".neptune/" in path:
                # Neptune stores async data locally before sync
                # Look for operation files that might contain run ID
                neptune_dir = path[: path.find(".neptune/") + 9]
                async_dir = Path(neptune_dir) / "async"
                if async_dir.exists():
                    # Run directories are named with the run ID
                    for run_dir in async_dir.iterdir():
                        if run_dir.is_dir():
                            info["run_id"] = run_dir.name
                            if project:
                                # Neptune URL format
                                workspace, proj = (
                                    project.split("/") if "/" in project else ("", project)
                                )
                                if workspace:
                                    info["url"] = (
                                        f"https://app.neptune.ai/{workspace}/{proj}/runs/{info['run_id']}"
                                    )
                            break
                break

        return info if len(info) > 1 else None
