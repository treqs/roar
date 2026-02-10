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
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def _get_tracer_mode(self) -> str:
        """Get the configured tracer mode (auto, ebpf, ptrace)."""
        try:
            from ...config import config_get

            mode = config_get("tracer.mode")
            if mode in ("auto", "ebpf", "ptrace"):
                return mode
        except Exception:
            pass
        return "auto"

    def _find_ptrace_tracer(self) -> str | None:
        """Find the roar-tracer (ptrace) binary."""
        candidates = [
            self._package_path.parent / "tracer" / "target" / "release" / "roar-tracer",
            self._package_path / "bin" / "roar-tracer",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        result = subprocess.run(["which", "roar-tracer"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def _find_ebpf_tracer(self) -> str | None:
        """Find the roar-tracer-ebpf binary."""
        candidates = [
            self._package_path.parent / "tracer-ebpf" / "target" / "release" / "roar-tracer-ebpf",
            self._package_path / "bin" / "roar-tracer-ebpf",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        result = subprocess.run(["which", "roar-tracer-ebpf"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def find_tracer(self) -> str | None:
        """
        Find the tracer binary based on configured mode.

        Mode behavior:
        - "ptrace": Only look for roar-tracer
        - "ebpf": Only look for roar-tracer-ebpf
        - "auto": Prefer roar-tracer-ebpf, fall back to roar-tracer

        Returns:
            Path to tracer binary, or None if not found
        """
        mode = self._get_tracer_mode()
        self.logger.debug("Tracer mode: %s", mode)

        if mode == "ptrace":
            self.logger.debug("Searching for ptrace tracer only")
            path = self._find_ptrace_tracer()
            if path:
                self.logger.debug("Found ptrace tracer: %s", path)
            return path

        if mode == "ebpf":
            self.logger.debug("Searching for eBPF tracer only")
            path = self._find_ebpf_tracer()
            if path:
                self.logger.debug("Found eBPF tracer: %s", path)
            return path

        # auto: prefer eBPF, fall back to ptrace
        self.logger.debug("Auto mode: trying eBPF first, then ptrace")
        path = self._find_ebpf_tracer()
        if path:
            self.logger.debug("Found eBPF tracer: %s", path)
            return path
        path = self._find_ptrace_tracer()
        if path:
            self.logger.debug("Falling back to ptrace tracer: %s", path)
        return path

    def execute(
        self,
        command: list[str],
        roar_dir: Path,
        signal_handler: ISignalHandler,
        extra_env: dict[str, str] | None = None,
    ) -> TracerResult:
        """
        Execute command with tracing.

        Args:
            command: Command and arguments to execute
            roar_dir: Path to .roar directory for log files
            signal_handler: Signal handler for interrupt management
            extra_env: Additional environment variables to set in the child process

        Returns:
            TracerResult with execution details

        Raises:
            RuntimeError: If tracer binary not found
        """
        self.logger.debug("TracerService.execute: command=%s", command)
        tracer_path = self.find_tracer()
        if not tracer_path:
            self.logger.debug("Tracer binary not found, raising error")
            mode = self._get_tracer_mode()
            if mode == "ebpf":
                hint = (
                    "roar-tracer-ebpf binary not found. Build it with:\n"
                    "  cd tracer-ebpf && cargo build --release"
                )
            elif mode == "ptrace":
                hint = (
                    "roar-tracer binary not found. Build it with:\n"
                    "  cd tracer && cargo build --release"
                )
            else:
                hint = (
                    "No tracer binary found. Build one with:\n"
                    "  cd tracer-ebpf && cargo build --release  (eBPF, recommended)\n"
                    "  cd tracer && cargo build --release        (ptrace, fallback)"
                )
            raise TracerNotFoundError(hint)

        # Generate log file paths
        pid = os.getpid()
        tracer_log_file = str(roar_dir / f"run_{pid}_tracer.msgpack")
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

        # Merge extra env (e.g. AWS_ENDPOINT_URL from proxy)
        if extra_env:
            env.update(extra_env)

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
        tracer_log = str(roar_dir / f"run_{pid}_tracer.msgpack")
        inject_log = str(roar_dir / f"run_{pid}_inject.json")
        return tracer_log, inject_log
