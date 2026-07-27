"""Analyzer for experiment tracking services (W&B, MLflow, Neptune)."""

import json
import re
import sqlite3
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
        "trackio": ["huggingface/trackio", "/trackio/"],
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

    @staticmethod
    def _all_written_files(context: dict) -> list:
        """Return unfiltered written files so tracker detection works even
        when tracker directories are in ignore_paths."""
        tracer_data = context.get("tracer_data", {})
        unfiltered = tracer_data.get("written_files", [])
        if unfiltered:
            return unfiltered
        return context.get("written_files", [])

    def relevant(self, context: dict) -> bool:
        """Check if any tracker directories were written to."""
        written = self._all_written_files(context)
        for path in written:
            for _tracker, patterns in self.TRACKER_PATTERNS.items():
                if any(p in path for p in patterns):
                    return True
        return False

    def analyze(self, context: dict) -> dict | None:
        written = self._all_written_files(context)
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
                # trackio reports one run per project DB (a run may touch
                # several); the other trackers return a single run dict.
                if isinstance(run_info, list):
                    results["runs"].extend(run_info)
                else:
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
        if "trackio" in trackers_found:
            results["ignore_patterns"].append("*/trackio/*")

        # Dedupe
        results["ignore_patterns"] = sorted(set(results["ignore_patterns"]))

        return results if results["trackers_detected"] else None

    def _extract_run_info(self, tracker: str, written_files: list, env: dict) -> dict | list | None:
        """Extract run URL and metadata for a specific tracker.

        Most trackers return a single run dict; ``trackio`` returns a list of
        per-project run dicts (or ``None`` when nothing usable was found).
        """
        if tracker == "wandb":
            return self._extract_wandb_info(written_files, env)
        elif tracker == "mlflow":
            return self._extract_mlflow_info(written_files, env)
        elif tracker == "trackio":
            return self._extract_trackio_info(written_files, env)
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
                # Extract run_id from directory name: run-YYYYMMDD_HHMMSS-<run_id>
                dir_match = re.match(r"run-\d{8}_\d{6}-(.+)$", run_dir.name)
                if dir_match:
                    info["run_id"] = dir_match.group(1)

                # Try to read run metadata for additional fields
                run_metadata = run_dir / "files" / "wandb-metadata.json"
                if run_metadata.exists():
                    try:
                        with open(run_metadata) as f:
                            metadata = json.load(f)
                        info["run_id"] = metadata.get("run_id")
                        info["project"] = metadata.get("project")
                        info["entity"] = metadata.get("entity")
                        if info.get("entity") and info.get("project") and info.get("run_id"):
                            info["url"] = (
                                f"https://wandb.ai/{info['entity']}/{info['project']}/runs/{info['run_id']}"
                            )
                    except (OSError, json.JSONDecodeError):
                        pass

                # Parse entity/project/run_id from debug.log.
                # wandb writes "finishing run <entity>/<project>/<run_id>"
                # to this file on every online run.
                debug_log = run_dir / "logs" / "debug.log"
                if debug_log.exists():
                    try:
                        text = debug_log.read_text(errors="replace")
                        finish_match = re.search(r"finishing run (\S+)/(\S+)/(\S+)", text)
                        if finish_match:
                            info["entity"] = finish_match.group(1)
                            info["project"] = finish_match.group(2)
                            info["run_id"] = finish_match.group(3)
                    except OSError:
                        pass

                # Check wandb-summary.json for runtime
                summary_file = run_dir / "files" / "wandb-summary.json"
                if summary_file.exists():
                    try:
                        with open(summary_file) as f:
                            summary = json.load(f)
                        wandb_info = summary.get("_wandb", {})
                        if "runtime" in wandb_info:
                            info["runtime_seconds"] = wandb_info["runtime"]
                    except (OSError, json.JSONDecodeError):
                        pass

        # Fall back to env vars if entity/project still missing
        if not info.get("entity"):
            info["entity"] = env.get("WANDB_ENTITY", "")
        if not info.get("project"):
            info["project"] = env.get("WANDB_PROJECT", "")

        # Build URL if we have all three components
        if info.get("entity") and info.get("project") and info.get("run_id"):
            info["url"] = (
                f"https://wandb.ai/{info['entity']}/{info['project']}/runs/{info['run_id']}"
            )

        return info if len(info) > 1 else None

    def _extract_trackio_info(self, written_files: list, env: dict) -> list | None:
        """Extract trackio run info + the hosted-dashboard URL — scrape only.

        trackio stores one SQLite DB per project at ``.../huggingface/trackio/
        <project>.db`` and, when a run syncs to a HF Space, records the Space in
        ``project_metadata`` (``key='space_id'``). We read that link + the run
        identity, exactly as the W&B extractor reads wandb's on-disk run URL — no
        roar-side config, no env, and **never the metrics**: TReqs stores no
        customer data, so the DAG carries a link to the externally-hosted
        experiment, and the metrics live on the dashboard.

        Returns ONE record per project DB (never splicing one project's identity
        onto another's Space), or ``None`` if no DB yielded anything usable. Each
        record's URL is ``https://huggingface.co/spaces/<space_id>?project=<project>``.
        (``env`` is unused — the space comes from what trackio itself persisted.)
        """
        del env
        db_paths = sorted({p for p in written_files if "trackio/" in p and p.endswith(".db")})

        runs: list[dict[str, Any]] = []
        for db_path in db_paths:
            path = Path(db_path)
            if not path.exists():
                continue
            info: dict[str, Any] = {"tracker": "trackio", "project": path.stem}
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                # Two INDEPENDENT lookups: an older/imported trackio schema may
                # lack a ``run_id`` column or the ``project_metadata`` table, so
                # a failure of one must not discard the other.
                run = self._trackio_run_identity(con)
                space = self._trackio_space_id(con)
            finally:
                con.close()

            if run:
                info["run_id"], info["run_name"] = run
            if space:
                info["space_id"] = space
                info["url"] = f"https://huggingface.co/spaces/{space}?project={info['project']}"

            # Keep only DBs that yielded a run identity and/or a hosted Space
            # (more than the always-present tracker+project keys).
            if len(info) > 2:
                runs.append(info)

        return runs or None

    @staticmethod
    def _trackio_run_identity(con: "sqlite3.Connection") -> tuple | None:
        """``(run_id, run_name)`` of the most recent metric row, tolerating
        older schemas whose ``metrics`` table lacks a ``run_id`` column (trackio
        produces these when importing TensorBoard/older runs)."""
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info(metrics)").fetchall()}
        except sqlite3.Error:
            return None
        if not cols or not ({"run_id", "run_name"} & cols):
            return None
        run_id_expr = "run_id" if "run_id" in cols else "NULL"
        run_name_expr = "run_name" if "run_name" in cols else "NULL"
        try:
            row = con.execute(
                f"SELECT {run_id_expr}, {run_name_expr} FROM metrics ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row or (row[0] is None and row[1] is None):
            return None
        return row[0], row[1]

    @staticmethod
    def _trackio_space_id(con: "sqlite3.Connection") -> str | None:
        """The hosted HF Space id trackio persisted once a run synced, or None."""
        try:
            row = con.execute(
                "SELECT value FROM project_metadata WHERE key = 'space_id' LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row and row[0] else None

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
