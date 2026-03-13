"""
Reproduction service for orchestrating artifact reproduction.

Extracted from reproduce.py to follow Single Responsibility Principle.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo, PipelineLookupResult
from ...presenters import NullPresenter
from ...utils.git_url import urls_match
from .environment_setup import EnvironmentSetupService
from .pipeline_executor import PipelineExecutor

if TYPE_CHECKING:
    from ...core.interfaces.presenter import IPresenter
    from ...integrations.glaas import GlaasClient


class ReproductionService:
    """
    Service for orchestrating artifact reproduction.

    Provides reproduction mechanics:
    - Artifact lookup (local or GLaaS)
    - Environment setup (git reuse or clone, venv, packages)
    - Pipeline execution

    Usage:
        service = ReproductionService(glaas_client, presenter)
        pipeline = service.lookup_pipeline_result("abc123", "https://glaas.example.com", Path(".roar"))
        environment = service.prepare_environment(pipeline.pipeline, Path.cwd(), auto_confirm=True)
        steps_run, steps_total = service.execute_pipeline(pipeline.pipeline, environment, auto_confirm=True)
    """

    def __init__(
        self,
        glaas_client: "GlaasClient | None" = None,
        presenter: "IPresenter | None" = None,
    ):
        """
        Initialize reproduction service.

        Args:
            glaas_client: GLaaS API client
            presenter: Presenter for user feedback
        """
        self._glaas = glaas_client
        self._presenter = presenter or NullPresenter()
        # Detect the roar executable once and pass to both services
        roar_exe = self._get_roar_executable()
        self._env_setup = EnvironmentSetupService(self._presenter, roar_executable=roar_exe)
        self._executor = PipelineExecutor(self._presenter, roar_executable=roar_exe)

    def prepare_environment(
        self,
        pipeline: PipelineInfo,
        cwd: Path,
        auto_confirm: bool,
        *,
        dpkg_any_version: bool = False,
        pip_any_version: bool = False,
        package_sync: bool = False,
    ) -> EnvironmentInfo:
        """Reuse or create a reproduction environment for the given pipeline."""
        environment = self._try_reuse_current_repo(cwd, pipeline)
        if environment is not None:
            return environment

        target_dir = cwd / "reproduce"
        self._print(f"\nSetting up environment in {target_dir}...")
        return self._env_setup.setup(
            pipeline,
            target_dir,
            auto_confirm,
            dpkg_any_version=dpkg_any_version,
            pip_any_version=pip_any_version,
            package_sync=package_sync,
        )

    def execute_pipeline(
        self,
        pipeline: PipelineInfo,
        environment: EnvironmentInfo,
        auto_confirm: bool,
    ) -> tuple[int, int]:
        """Execute recorded pipeline steps in the prepared environment."""
        return self._executor.execute(
            pipeline,
            environment,
            auto_confirm,
        )

    def _try_reuse_current_repo(
        self,
        cwd: Path,
        pipeline: PipelineInfo,
    ) -> EnvironmentInfo | None:
        """
        Check if cwd is inside a git repo whose origin matches the pipeline remote.

        If so, checkout the target commit and return an EnvironmentInfo for
        the existing repo, avoiding a fresh clone.

        Returns:
            EnvironmentInfo if the current repo can be reused, None otherwise.
        """
        try:
            repo_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                cwd=cwd,
                check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

        try:
            origin_url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=repo_root,
                check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

        if not pipeline.git_repo or not urls_match(origin_url, pipeline.git_repo):
            return None

        self._print("Current repository matches artifact remote, using existing environment")

        repo_dir = Path(repo_root)

        # Checkout the target commit if specified
        if pipeline.git_commit:
            try:
                subprocess.run(
                    ["git", "checkout", pipeline.git_commit],
                    capture_output=True,
                    text=True,
                    cwd=repo_root,
                    check=True,
                )
            except subprocess.CalledProcessError:
                self._print(
                    f"Warning: could not checkout commit {pipeline.git_commit}, "
                    "continuing with current HEAD"
                )

        venv_dir = repo_dir / ".venv" if (repo_dir / ".venv").is_dir() else None

        return EnvironmentInfo(
            repo_dir=repo_dir,
            venv_dir=venv_dir,
            python_version=None,
        )

    def _lookup_pipeline(
        self,
        hash_prefix: str,
        server_url: str | None,
        roar_dir: Path,
    ) -> tuple[PipelineInfo | None, str | None]:
        """Backward-compatible tuple wrapper around lookup_pipeline_result()."""
        result = self.lookup_pipeline_result(hash_prefix, server_url, roar_dir)
        return result.pipeline, result.error

    def lookup_pipeline_result(
        self,
        hash_prefix: str,
        server_url: str | None,
        roar_dir: Path,
    ) -> PipelineLookupResult:
        """
        Look up artifact and retrieve pipeline info.

        First tries local database, then GLaaS if configured.

        Returns:
            Structured lookup result with pipeline + error + source.
        """
        # Try local lookup first
        pipeline = self._lookup_local(hash_prefix, roar_dir)
        if pipeline:
            return PipelineLookupResult(pipeline=pipeline, error=None, source="local")

        # Try GLaaS
        if self._glaas or server_url:
            pipeline, error = self._lookup_remote(hash_prefix, server_url)
            if error:
                return PipelineLookupResult(pipeline=None, error=error, source="remote")
            if pipeline:
                return PipelineLookupResult(pipeline=pipeline, error=None, source="remote")

        return PipelineLookupResult(
            pipeline=None,
            error=(
                f"Artifact not found: {hash_prefix}\n"
                "If this artifact is on a remote server, check your authentication with 'roar auth test'."
            ),
            source="none",
        )

    def _lookup_local(
        self,
        hash_prefix: str,
        roar_dir: Path,
    ) -> PipelineInfo | None:
        """Look up artifact and pipeline in local database."""
        from ...db.context import create_database_context

        with create_database_context(roar_dir) as ctx:
            # Find artifact by hash prefix
            artifact = ctx.artifacts.get_by_hash(hash_prefix)
            if not artifact:
                return None

            artifact_hash = None
            for h in artifact.get("hashes", []):
                if h.get("algorithm") == "blake3":
                    artifact_hash = h.get("digest")
                    break

            if not artifact_hash:
                return None

            # Get producer job
            jobs = ctx.artifacts.get_jobs(artifact["id"])
            producers = jobs.get("produced_by", [])
            if not producers:
                return None

            producer = producers[0]

            # Get session for the producer
            session_id = producer.get("session_id")
            if not session_id:
                return None

            session = ctx.sessions.get(session_id)
            if not session:
                return None

            # Get all steps from the session
            steps = ctx.sessions.get_steps(session_id)

            build_steps = []
            run_steps = []

            for step in steps:
                step_dict = dict(step)
                # Add inputs/outputs
                inputs = ctx.jobs.get_inputs(step["id"])
                outputs = ctx.jobs.get_outputs(step["id"])
                step_dict["_inputs"] = inputs
                step_dict["_outputs"] = outputs

                if step.get("job_type") == "build":
                    build_steps.append(step_dict)
                else:
                    run_steps.append(step_dict)

            return PipelineInfo(
                artifact_hash=artifact_hash,
                git_repo=session.get("git_repo"),
                git_commit=session.get("git_commit_start") or session.get("git_commit_end"),
                build_steps=build_steps,
                run_steps=run_steps,
                total_steps=len(build_steps) + len(run_steps),
            )

    def _lookup_remote(
        self,
        hash_prefix: str,
        server_url: str | None,
    ) -> tuple[PipelineInfo | None, str | None]:
        """Look up artifact and pipeline from GLaaS."""
        client = self._glaas

        if server_url and not client:
            from ...integrations.glaas import GlaasClient

            client = GlaasClient(server_url)

        if not client:
            return None, "No GLaaS server configured"

        # Get artifact info
        try:
            from ...core.exceptions import GlaasApiError

            artifact = client.get_artifact(hash_prefix)
        except GlaasApiError as e:
            return None, str(e)
        except (ValueError, RuntimeError) as e:
            return None, str(e)  # Propagate the actual error
        if not artifact:
            return None, None  # Not found, not an error

        # Get pipeline
        pipeline_data, error = client.get_artifact_dag(hash_prefix)
        if error:
            return None, error
        if not pipeline_data:
            return None, None

        build_steps = []
        run_steps = []

        for job in pipeline_data.get("jobs", []):
            if job.get("jobType") == "build":
                build_steps.append(job)
            else:
                run_steps.append(job)

        return PipelineInfo(
            artifact_hash=artifact.get("hash") or hash_prefix,
            git_repo=pipeline_data.get("gitRepo"),
            git_commit=pipeline_data.get("gitCommit"),
            build_steps=build_steps,
            run_steps=run_steps,
            total_steps=len(build_steps) + len(run_steps),
        ), None

    def _print(self, message: str) -> None:
        """Print message through the configured presenter."""
        self._presenter.print(message)

    def _get_roar_executable(self) -> str:
        """Get path to the currently running roar executable.

        Returns:
            Path to roar executable, or fallback to python -m roar
        """
        # Option 1: If roar is installed as a script on PATH
        roar_path = shutil.which("roar")
        if roar_path:
            return roar_path
        # Option 2: Use current Python to run roar module
        return f"{sys.executable} -m roar"
