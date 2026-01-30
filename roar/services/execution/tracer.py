"""
Tracer service for process execution with file I/O tracking.

Handles tracer binary discovery and process execution via the tracer.
"""

import os
import subprocess
import time
from pathlib import Path

from ...core.exceptions import TracerNotFoundError
from ...core.interfaces.logger import ILogger
from ...core.interfaces.run import ISignalHandler, TracerResult


class TracerService:
    """
    Manages tracer discovery and execution.

    Follows SRP: only handles process tracing.
    Follows OCP: tracer discovery can be extended.
    """

    def __init__(self, package_path: Path | None = None, logger: ILogger | None = None) -> None:
        """
        Initialize tracer service.

        Args:
            package_path: Path to the roar package (for finding tracer binary)
            logger: Logger for internal diagnostics
        """
        # Go up 3 levels: execution -> services -> roar
        self._package_path = package_path or Path(__file__).parent.parent.parent
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

    def find_tracer(self) -> str | None:
        """
        Find the roar-tracer binary.

        Searches in:
        1. Development location (tracer/target/release/)
        2. Installed location (roar/bin/)
        3. System PATH

        Returns:
            Path to tracer binary, or None if not found
        """
        self.logger.debug("Searching for roar-tracer binary")
        candidates = [
            # Development: relative to roar package
            self._package_path.parent / "tracer" / "target" / "release" / "roar-tracer",
            # Installed alongside roar package
            self._package_path / "bin" / "roar-tracer",
        ]

        for candidate in candidates:
            self.logger.debug("Checking tracer path: %s", candidate)
            if candidate.exists():
                self.logger.debug("Found tracer at: %s", candidate)
                return str(candidate)

        # Check if it's in PATH
        self.logger.debug("Checking system PATH for roar-tracer")
        result = subprocess.run(["which", "roar-tracer"], capture_output=True, text=True)
        if result.returncode == 0:
            path = result.stdout.strip()
            self.logger.debug("Found tracer in PATH: %s", path)
            return path

        self.logger.debug("Tracer binary not found")
        return None

    def execute(
        self,
        command: list[str],
        roar_dir: Path,
        signal_handler: ISignalHandler,
    ) -> TracerResult:
        """
        Execute command with tracing.

        Args:
            command: Command and arguments to execute
            roar_dir: Path to .roar directory for log files
            signal_handler: Signal handler for interrupt management

        Returns:
            TracerResult with execution details

        Raises:
            RuntimeError: If tracer binary not found
        """
        self.logger.debug("TracerService.execute: command=%s", command)
        tracer_path = self.find_tracer()
        if not tracer_path:
            self.logger.debug("Tracer binary not found, raising error")
            raise TracerNotFoundError(
                "roar-tracer binary not found. Please build it with:\n"
                "  cd roar/tracer && cargo build --release"
            )

        # Generate log file paths
        pid = os.getpid()
        tracer_log_file = str(roar_dir / f"run_{pid}_tracer.json")
        inject_log_file = str(roar_dir / f"run_{pid}_inject.json")
        self.logger.debug("Log files: tracer=%s, inject=%s", tracer_log_file, inject_log_file)

        # Update signal handler with log files for cleanup on abort
        signal_handler.set_log_files([tracer_log_file, inject_log_file])

        # Prepare environment for child process
        env = dict(os.environ)

        # Inject persistent env vars from .roar/config.toml [env] section
        try:
            from ...config import load_config

            config = load_config()
            config_env = config.get("env", {})
            if isinstance(config_env, dict):
                env.update(config_env)
        except Exception:
            pass  # Best-effort
        # inject/ is now in the same directory as this file
        inject_dir = str(Path(__file__).parent / "inject")
        env["PYTHONPATH"] = inject_dir + os.pathsep + env.get("PYTHONPATH", "")
        env["ROAR_LOG_FILE"] = inject_log_file

        # Build tracer command
        tracer_cmd = [tracer_path, tracer_log_file, *command]
        self.logger.debug("Tracer command: %s", tracer_cmd)

        # Execute with signal handling
        self.logger.debug("Installing signal handler and starting process")
        start_time = time.time()
        signal_handler.install()

        try:
            proc = subprocess.Popen(tracer_cmd, env=env)
            self.logger.debug("Process started: pid=%d", proc.pid)
            exit_code = proc.wait()
            self.logger.debug("Process exited: code=%d", exit_code)
        except KeyboardInterrupt:
            # This shouldn't happen since we handle SIGINT, but just in case
            self.logger.debug("KeyboardInterrupt caught during wait")
            exit_code = proc.wait()
        finally:
            signal_handler.restore()
            self.logger.debug("Signal handler restored")

        end_time = time.time()
        duration = end_time - start_time
        self.logger.debug(
            "Execution completed: duration=%.2fs, interrupted=%s",
            duration,
            signal_handler.is_interrupted(),
        )

        return TracerResult(
            exit_code=exit_code,
            duration=end_time - start_time,
            tracer_log_path=tracer_log_file,
            inject_log_path=inject_log_file,
            interrupted=signal_handler.is_interrupted(),
        )

    def get_log_paths(self, roar_dir: Path) -> tuple:
        """
        Get log file paths for a run.

        Args:
            roar_dir: Path to .roar directory

        Returns:
            Tuple of (tracer_log_path, inject_log_path)
        """
        pid = os.getpid()
        tracer_log = str(roar_dir / f"run_{pid}_tracer.json")
        inject_log = str(roar_dir / f"run_{pid}_inject.json")
        return tracer_log, inject_log
