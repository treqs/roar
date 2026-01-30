"""
Reproduction service for orchestrating artifact reproduction.

Extracted from reproduce.py to follow Single Responsibility Principle.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo, ReproductionResult
from ...utils.git_url import urls_match
from .environment_setup import EnvironmentSetupService
from .pipeline_executor import PipelineExecutor

if TYPE_CHECKING:
    from ...core.interfaces.presenter import IPresenter
    from ...glaas_client import GlaasClient


class ReproductionService:
    """
    Service for orchestrating artifact reproduction.

    Coordinates:
    - Artifact lookup (local or GLaaS)
    - Pipeline retrieval
    - Environment setup (git clone, venv, packages)
    - Pipeline execution

    Usage:
        service = ReproductionService(glaas_client, presenter)
        result = service.reproduce(
            hash_prefix="abc123",
            server_url="https://glaas.example.com",
            run_pipeline=True,
            auto_confirm=True,
            roar_dir=Path(".roar"),
            cwd=Path.cwd(),
        )
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
        self._presenter = presenter
        # Detect the roar executable once and pass to both services
        roar_exe = self._get_roar_executable()
        self._env_setup = EnvironmentSetupService(presenter, roar_executable=roar_exe)
        self._executor = PipelineExecutor(presenter, roar_executable=roar_exe)

    def reproduce(
        self,
        hash_prefix: str,
        server_url: str | None,
        run_pipeline: bool,
        auto_confirm: bool,
        roar_dir: Path,
        cwd: Path,
        dpkg_any_version: bool = False,
        pip_any_version: bool = False,
        package_sync: bool = False,
        list_requirements: bool = False,
    ) -> ReproductionResult:
        """
        Reproduce an artifact from its hash.

        Args:
            hash_prefix: Artifact hash prefix to reproduce
            server_url: GLaaS server URL (overrides config)
            run_pipeline: Whether to run the pipeline after setup
            auto_confirm: Auto-confirm prompts
            roar_dir: Path to .roar directory
            cwd: Current working directory

        Returns:
            ReproductionResult with success status
        """
        warnings: list[str] = []

        # Look up artifact and get pipeline
        pipeline, error = self._lookup_pipeline(hash_prefix, server_url, roar_dir)
        if error:
            return ReproductionResult(success=False, error=error)

        if not pipeline:
            return ReproductionResult(
                success=False,
                error=f"No pipeline found for artifact {hash_prefix}",
            )

        # Show pipeline preview
        self._print(f"Found artifact: {pipeline.artifact_hash}")
        self._print(f"Git repo: {pipeline.git_repo or 'Not available'}")
        self._print(f"Git commit: {pipeline.git_commit or 'Not available'}")
        self._print(f"Build steps: {len(pipeline.build_steps)}")
        self._print(f"Run steps: {len(pipeline.run_steps)}")

        # Check if we can reproduce
        if not pipeline.git_repo:
            return ReproductionResult(
                success=False,
                error="Cannot reproduce: no git repository URL available",
            )

        # Show full package lists if requested
        if list_requirements:
            build_dpkg_packages = self._env_setup._get_build_dpkg_packages(pipeline)
            dpkg_packages = self._env_setup._get_dpkg_packages(pipeline)
            pip_packages = self._env_setup._get_packages(pipeline)
            if build_dpkg_packages:
                self._print(f"\nBuild tool packages ({len(build_dpkg_packages)}):")
                for name in sorted(build_dpkg_packages):
                    self._print(f"  - {name}")
            if dpkg_packages:
                self._print(f"\nSystem packages ({len(dpkg_packages)}):")
                for name in sorted(dpkg_packages):
                    self._print(f"  - {name}")
            if pip_packages:
                self._print(f"\nPip packages ({len(pip_packages)}):")
                for pkg in sorted(pip_packages):
                    self._print(f"  - {pkg}")

        # Confirm reproduction
        if not auto_confirm:
            if self._presenter:
                if not self._presenter.confirm("Proceed with reproduction?", default=True):
                    return ReproductionResult(
                        success=False,
                        error="Reproduction cancelled by user",
                    )
            else:
                response = input("Proceed with reproduction? [Y/n] ")
                if response.lower() == "n":
                    return ReproductionResult(
                        success=False,
                        error="Reproduction cancelled by user",
                    )

        # Check if current repo matches the artifact remote
        environment = self._try_reuse_current_repo(cwd, pipeline)

        if environment is None:
            # Set up environment via clone
            target_dir = cwd / "reproduce"
            self._print(f"\nSetting up environment in {target_dir}...")

            try:
                environment = self._env_setup.setup(
                    pipeline,
                    target_dir,
                    auto_confirm,
                    dpkg_any_version=dpkg_any_version,
                    pip_any_version=pip_any_version,
                    package_sync=package_sync,
                )
            except RuntimeError as e:
                return ReproductionResult(
                    success=False,
                    error=f"Environment setup failed: {e}",
                )

        self._print(f"Environment ready: {environment.repo_dir}")

        # Execute pipeline if requested
        steps_run = 0
        steps_total = pipeline.total_steps

        if run_pipeline:
            self._executor.preview_steps(pipeline)

            if not auto_confirm:
                if self._presenter:
                    if not self._presenter.confirm("Run the pipeline?", default=True):
                        return ReproductionResult(
                            success=True,
                            repo_dir=environment.repo_dir,
                            steps_run=0,
                            steps_total=steps_total,
                            warnings=["Pipeline not executed (user chose to skip)"],
                        )
                else:
                    response = input("Run the pipeline? [Y/n] ")
                    if response.lower() == "n":
                        return ReproductionResult(
                            success=True,
                            repo_dir=environment.repo_dir,
                            steps_run=0,
                            steps_total=steps_total,
                            warnings=["Pipeline not executed (user chose to skip)"],
                        )

            steps_run, steps_total = self._executor.execute(
                pipeline,
                environment,
                auto_confirm,
            )

        return ReproductionResult(
            success=True,
            repo_dir=environment.repo_dir,
            steps_run=steps_run,
            steps_total=steps_total,
            warnings=warnings,
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
        """
        Look up artifact and retrieve pipeline info.

        First tries local database, then GLaaS if configured.

        Returns:
            Tuple of (PipelineInfo, None) or (None, error_message)
        """
        # Try local lookup first
        pipeline = self._lookup_local(hash_prefix, roar_dir)
        if pipeline:
            return pipeline, None

        # Try GLaaS
        if self._glaas or server_url:
            pipeline, error = self._lookup_remote(hash_prefix, server_url)
            if error:
                return None, error
            if pipeline:
                return pipeline, None

        return None, (
            f"Artifact not found: {hash_prefix}\n"
            "If this artifact is on a remote server, check your authentication with 'roar auth test'."
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
                inputs = ctx.jobs.get_inputs(step["id"], ctx.artifacts)
                outputs = ctx.jobs.get_outputs(step["id"], ctx.artifacts)
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
            from ...glaas_client import GlaasClient

            client = GlaasClient(server_url)

        if not client:
            return None, "No GLaaS server configured"

        # Get artifact info
        artifact, artifact_error = client.get_artifact(hash_prefix)
        if artifact_error:
            return None, artifact_error  # Propagate the actual error
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
        """Print message via presenter or fallback to print."""
        if self._presenter:
            self._presenter.print(message)
        else:
            print(message)

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
