"""
Pipeline executor service for reproduction.

Extracted from reproduce.py to follow Single Responsibility Principle.
This service handles executing pipeline steps during reproduction.
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from ...application.tags import run_modifier_flags
from ...presenters import NullPresenter

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
        step_timeout: int | None = None,
    ):
        """
        Initialize pipeline executor.

        Args:
            presenter: Presenter for user feedback
            roar_executable: Path to roar executable (auto-detected if not provided)
            step_timeout: Per-step wall-clock timeout in seconds; ``None`` (default)
                means no timeout.
        """
        self._presenter = presenter or NullPresenter()
        self._roar_initialized = False
        self._roar_executable = roar_executable or self._detect_roar_executable()
        self._step_timeout = step_timeout

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
                    if not self._presenter.confirm(f"Run: {command}?", default=True):
                        self._print("Step skipped.")
                        continue

                success = self._run_step(step, environment, is_build=False)
                if success:
                    steps_run += 1
                else:
                    self._print(f"Step {i} failed.")
                    if not auto_confirm:
                        cont = self._presenter.confirm("Continue with next step?", default=True)
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

        # Parse step metadata once: env vars + the recorded `roar run` modifiers
        # (--block-tag / --add-tag) that shaped the original tag/barrier layer.
        metadata = step.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        step_env_vars: dict[str, str] = metadata.get("env_vars", {})
        modifier_flags = run_modifier_flags(metadata.get("run_modifiers"))

        # Wrap with roar for provenance tracking, replaying recorded modifiers so
        # the reproduced run reproduces the same tags/barriers, not just bytes.
        roar_cmd = "build" if is_build else "run"
        wrapped_command = self._wrap_with_roar(
            command, roar_cmd, environment, modifiers=modifier_flags
        )

        shown = f"{modifier_flags} {command}".strip()
        self._print(f"  Command: roar {roar_cmd} {shown}")

        # Set up environment
        env = self._prepare_environment(environment, env_vars=step_env_vars)

        # Run the command in its own session/process group so that, if a timeout
        # fires, we can kill the whole tree. shell=True means the direct child is
        # a shell whose grandchild (e.g. train.py) would be orphaned by a plain
        # kill of the shell — leaving a workload running on the GPU past the
        # declared failure. `timeout` defaults to None (no timeout): a run should
        # not be capped at an arbitrary wall-clock that also makes the row only
        # reproducible on hardware at least as fast as the machine that made it.
        try:
            # Note: Using shell=True for complex commands with pipes, etc.
            proc = subprocess.Popen(
                wrapped_command,
                shell=True,
                cwd=environment.repo_dir,
                env=env,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=self._step_timeout)
            except subprocess.TimeoutExpired:
                self._print(
                    f"  Step timed out after {self._step_timeout}s — killing the process group"
                )
                self._kill_process_group(proc)
                return False

            if returncode == 0:
                self._print("  Success")
                return True
            else:
                self._print(f"  Failed with exit code {returncode}")
                return False

        except Exception as e:
            self._print(f"  Error: {e}")
            return False

    @staticmethod
    def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
        """SIGKILL the step's whole process group, then reap it.

        With ``shell=True`` the workload is a grandchild of the shell, so killing
        only ``proc`` leaves it orphaned (a false failure + a silent GPU-cost
        leak on someone else's bill). ``start_new_session=True`` gives the step
        its own group, which we kill here.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)

    def _wrap_with_roar(
        self,
        command: str,
        roar_cmd: str,
        environment: "EnvironmentInfo",
        modifiers: str = "",
    ) -> str:
        """Wrap a command with roar build/run.

        Uses the external roar executable (from the parent process) instead of
        installing roar in the reproduce venv. This prevents roar from being
        deleted if a build step runs 'uv sync'. *modifiers* are recorded
        ``roar run`` flags (e.g. ``--block-tag …``) replayed for fidelity.
        """
        prefix = f"{self._roar_executable} {roar_cmd}"
        if modifiers:
            prefix = f"{prefix} {modifiers}"
        return f"{prefix} {command}"

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

        # Tell the inner `roar run` it's reproducing, so it doesn't enforce the
        # clean-tree rule on outputs this very reproduction is recreating
        # (otherwise step 1's output blocks step 2 — a self-deadlock).
        env["ROAR_REPRODUCE"] = "1"

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
        """Print message through the configured presenter."""
        self._presenter.print(message)
