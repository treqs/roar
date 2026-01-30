"""
Pipeline executor service for reproduction.

Extracted from reproduce.py to follow Single Responsibility Principle.
This service handles executing pipeline steps during reproduction.
"""

import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.interfaces.presenter import IPresenter
    from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo


class PipelineExecutor:
    """
    Service for executing reproduction pipeline steps.

    Handles:
    - Build step execution (in order)
    - Run step execution (in order)
    - Environment activation
    - Error handling and progress tracking

    Usage:
        executor = PipelineExecutor(presenter)
        steps_run, steps_total = executor.execute(pipeline, environment, auto_confirm=True)
    """

    def __init__(
        self,
        presenter: "IPresenter | None" = None,
        roar_executable: str | None = None,
    ):
        """
        Initialize pipeline executor.

        Args:
            presenter: Presenter for user feedback
            roar_executable: Path to roar executable (auto-detected if not provided)
        """
        self._presenter = presenter
        self._roar_initialized = False
        self._roar_executable = roar_executable or self._detect_roar_executable()

    def execute(
        self,
        pipeline: "PipelineInfo",
        environment: "EnvironmentInfo",
        auto_confirm: bool = False,
    ) -> tuple[int, int]:
        """
        Execute pipeline steps.

        Runs build steps first, then run steps, in their recorded order.

        Args:
            pipeline: Pipeline to execute
            environment: Execution environment with venv path
            auto_confirm: Skip confirmation prompts

        Returns:
            Tuple of (steps_run, steps_total)
        """
        total_steps = len(pipeline.build_steps) + len(pipeline.run_steps)
        steps_run = 0

        # Run build steps first
        if pipeline.build_steps:
            self._print(f"\nRunning {len(pipeline.build_steps)} build step(s)...")
            for i, step in enumerate(pipeline.build_steps, 1):
                self._print(f"\n[Build {i}/{len(pipeline.build_steps)}]")
                success = self._run_step(step, environment, is_build=True)
                if success:
                    steps_run += 1
                else:
                    self._print(f"Build step {i} failed, stopping.")
                    return steps_run, total_steps

        # Run pipeline steps
        if pipeline.run_steps:
            self._print(f"\nRunning {len(pipeline.run_steps)} pipeline step(s)...")
            for i, step in enumerate(pipeline.run_steps, 1):
                self._print(f"\n[Step {i}/{len(pipeline.run_steps)}]")

                # Ask for confirmation if not auto
                if not auto_confirm:
                    command = step.get("command", "")
                    if self._presenter:
                        if not self._presenter.confirm(f"Run: {command}?", default=True):
                            self._print("Step skipped.")
                            continue
                    else:
                        response = input(f"Run: {command}? [Y/n] ")
                        if response.lower() == "n":
                            self._print("Step skipped.")
                            continue

                success = self._run_step(step, environment, is_build=False)
                if success:
                    steps_run += 1
                else:
                    self._print(f"Step {i} failed.")
                    if not auto_confirm:
                        if self._presenter:
                            cont = self._presenter.confirm("Continue with next step?", default=True)
                        else:
                            response = input("Continue with next step? [Y/n] ")
                            cont = response.lower() != "n"
                        if not cont:
                            break

        return steps_run, total_steps

    def _run_step(
        self,
        step: dict,
        environment: "EnvironmentInfo",
        is_build: bool = False,
    ) -> bool:
        """
        Run a single pipeline step.

        Returns:
            True if step succeeded
        """
        command = step.get("command", "")
        if not command:
            self._print("  No command found for step, skipping.")
            return True

        # Wrap with roar for provenance tracking
        roar_cmd = "build" if is_build else "run"
        wrapped_command = self._wrap_with_roar(command, roar_cmd, environment)

        self._print(f"  Command: roar {roar_cmd} {command}")

        # Extract env vars from step metadata
        step_env_vars: dict[str, str] = {}
        metadata = step.get("metadata")
        if metadata:
            import json as _json

            if isinstance(metadata, str):
                try:
                    metadata = _json.loads(metadata)
                except (ValueError, TypeError):
                    metadata = {}
            if isinstance(metadata, dict):
                step_env_vars = metadata.get("env_vars", {})

        # Set up environment
        env = self._prepare_environment(environment, env_vars=step_env_vars)

        # Run the command
        try:
            # Note: Using shell=True for complex commands with pipes, etc.
            result = subprocess.run(
                wrapped_command,
                shell=True,
                cwd=environment.repo_dir,
                env=env,
                timeout=3600,  # 1 hour timeout
            )

            if result.returncode == 0:
                self._print("  Success")
                return True
            else:
                self._print(f"  Failed with exit code {result.returncode}")
                return False

        except subprocess.TimeoutExpired:
            self._print("  Step timed out after 1 hour")
            return False
        except Exception as e:
            self._print(f"  Error: {e}")
            return False

    def _wrap_with_roar(
        self,
        command: str,
        roar_cmd: str,
        environment: "EnvironmentInfo",
    ) -> str:
        """Wrap a command with roar build/run.

        Uses the external roar executable (from the parent process) instead of
        installing roar in the reproduce venv. This prevents roar from being
        deleted if a build step runs 'uv sync'.
        """
        return f"{self._roar_executable} {roar_cmd} {command}"

    def _detect_roar_executable(self) -> str:
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

    def _get_venv_python(self, venv_dir) -> str:
        """Get path to Python executable in venv."""
        if sys.platform == "win32":
            return str(venv_dir / "Scripts" / "python.exe")
        return str(venv_dir / "bin" / "python")

    def _prepare_environment(
        self,
        environment: "EnvironmentInfo",
        env_vars: dict[str, str] | None = None,
    ) -> dict:
        """
        Prepare environment variables for step execution.

        Activates virtual environment by modifying PATH.
        """
        env = os.environ.copy()

        if environment.venv_dir:
            # Add venv bin to PATH
            if sys.platform == "win32":
                venv_bin = environment.venv_dir / "Scripts"
            else:
                venv_bin = environment.venv_dir / "bin"

            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(environment.venv_dir)

            # Remove PYTHONHOME if set (can interfere with venv)
            env.pop("PYTHONHOME", None)

        # Inject env vars from step metadata
        if env_vars:
            env.update(env_vars)

        return env

    def preview_steps(self, pipeline: "PipelineInfo") -> None:
        """
        Preview pipeline steps without executing.

        Args:
            pipeline: Pipeline to preview
        """
        self._print("\nPipeline Preview")
        self._print("=" * 40)

        if pipeline.build_steps:
            self._print(f"\nBuild Steps ({len(pipeline.build_steps)}):")
            for i, step in enumerate(pipeline.build_steps, 1):
                self._print(f"  B{i}. {step.get('command', 'No command')}")

        if pipeline.run_steps:
            self._print(f"\nRun Steps ({len(pipeline.run_steps)}):")
            for i, step in enumerate(pipeline.run_steps, 1):
                self._print(f"  {i}. {step.get('command', 'No command')}")

        self._print("")

    def _print(self, message: str) -> None:
        """Print message via presenter or fallback to print."""
        if self._presenter:
            self._presenter.print(message)
        else:
            print(message)
