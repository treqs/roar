"""
Tracer service for process execution with file I/O tracking.

Handles tracer binary discovery and process execution via the tracer.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from ...core.exceptions import TracerNotFoundError
from ...core.interfaces.logger import ILogger
from ...core.interfaces.run import ISignalHandler, TracerResult
from ...core.tracer_modes import TRACER_BACKEND_ORDER, is_valid_tracer_mode
from . import tracer_backends


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
        self._preload_bootstrap_attempted = False

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def _get_tracer_mode(self) -> str:
        """Get the configured tracer mode (auto, ebpf, preload, ptrace)."""
        try:
            from ...config import config_get

            mode = config_get("tracer.default")
            if isinstance(mode, str) and is_valid_tracer_mode(mode):
                return mode
        except Exception:
            pass
        return "auto"

    def _get_fallback_enabled(self) -> bool:
        """Get whether tracer fallback is enabled."""
        try:
            from ...config import config_get

            value = config_get("tracer.fallback_enabled")
            if isinstance(value, bool):
                return value
        except Exception:
            pass
        return True

    def _find_ptrace_tracer(self) -> str | None:
        """Find the roar-tracer (ptrace) binary."""
        return tracer_backends.find_ptrace_tracer(self._package_path)

    def _find_ebpf_tracer(self) -> str | None:
        """Find the roar-tracer-ebpf binary."""
        return tracer_backends.find_ebpf_tracer(self._package_path)

    def _find_preload_tracer(self) -> str | None:
        """Find the roar-tracer-preload launcher binary."""
        return tracer_backends.find_preload_tracer(self._package_path)

    def _find_preload_library(self) -> str | None:
        """Find the preload interposer shared library."""
        return tracer_backends.find_preload_library(self._package_path)

    def _get_perf_event_paranoid(self) -> int | None:
        """Read perf_event_paranoid (Linux only)."""
        return tracer_backends.get_perf_event_paranoid()

    def _get_binary_caps(self, path: str) -> set[str] | None:
        """Read Linux capabilities from a binary via getcap."""
        return tracer_backends.get_binary_caps(path)

    def _ebpf_is_ready(self, path: str) -> tuple[bool, str | None]:
        """
        Check whether eBPF tracer is likely to start.

        Returns:
            (is_ready, reason_if_not_ready)
        """
        return tracer_backends.ebpf_readiness(path).as_tuple()

    def _preload_is_ready(self, launcher_path: str) -> tuple[bool, str | None]:
        """Check whether preload launcher and library are available."""
        return tracer_backends.preload_readiness(self._package_path, launcher_path).as_tuple()

    def _get_tracer_candidates(
        self,
        mode: str,
        fallback_enabled: bool,
    ) -> list[tuple[str, str]]:
        """
        Resolve ordered tracer candidates as (backend_name, binary_path).

        In auto mode we include only backends likely to be usable.
        """
        if not is_valid_tracer_mode(mode):
            self.logger.warning("Unknown tracer mode %r, falling back to auto", mode)
            mode = "auto"

        candidates: list[tuple[str, str]] = []

        if mode == "auto":
            for backend in TRACER_BACKEND_ORDER:
                candidate = self._resolve_backend_candidate(backend, require_ready=True)
                if candidate:
                    candidates.append(candidate)
        else:
            candidate = self._resolve_backend_candidate(mode, require_ready=False)
            if candidate:
                candidates.append(candidate)

        if not fallback_enabled and candidates:
            return [candidates[0]]
        return candidates

    def _resolve_backend_candidate(
        self,
        backend: str,
        require_ready: bool,
    ) -> tuple[str, str] | None:
        if backend == "ebpf":
            ebpf = self._find_ebpf_tracer()
            if not ebpf:
                return None
            if require_ready:
                ready, reason = self._ebpf_is_ready(ebpf)
                if not ready:
                    self.logger.debug("Skipping eBPF tracer in auto mode: %s", reason)
                    return None
            return "ebpf", ebpf

        if backend == "preload":
            preload = self._find_preload_tracer()
            if not preload:
                self._attempt_preload_bootstrap()
                preload = self._find_preload_tracer()
            if not preload:
                return None
            if require_ready:
                ready, reason = self._preload_is_ready(preload)
                if not ready:
                    if reason == "preload library not found":
                        self._attempt_preload_bootstrap()
                        ready, reason = self._preload_is_ready(preload)
                    if not ready:
                        self.logger.debug("Skipping preload tracer in auto mode: %s", reason)
                        return None
            return "preload", preload

        if backend == "ptrace":
            ptrace = self._find_ptrace_tracer()
            if ptrace:
                return "ptrace", ptrace
        return None

    def _attempt_preload_bootstrap(self) -> None:
        """
        Build preload tracer artifacts once per process on macOS source checkouts.
        """
        if self._preload_bootstrap_attempted:
            return
        self._preload_bootstrap_attempted = True

        if sys.platform != "darwin":
            return

        print("🪚 building tracer", flush=True)
        self.logger.info("Preload tracer not found; attempting local build via cargo")
        ok = tracer_backends.build_preload_tracer(self._package_path)
        if ok:
            self.logger.info("Built roar-tracer-preload successfully")
        else:
            self.logger.debug("Automatic preload tracer build did not succeed")

    def find_tracer(self) -> str | None:
        """
        Find the tracer binary based on configured mode.

        Mode behavior:
        - "ptrace": Only look for roar-tracer
        - "preload": Only look for roar-tracer-preload
        - "ebpf": Only look for roar-tracer-ebpf
        - "auto": Prefer roar-tracer-ebpf, then preload, then roar-tracer

        Returns:
            Path to tracer binary, or None if not found
        """
        mode = self._get_tracer_mode()
        fallback_enabled = self._get_fallback_enabled()
        self.logger.debug("Tracer mode: %s (fallback_enabled=%s)", mode, fallback_enabled)

        candidates = self._get_tracer_candidates(mode, fallback_enabled)
        if candidates:
            backend, path = candidates[0]
            self.logger.debug("Selected %s tracer: %s", backend, path)
            return path
        return None

    def execute(
        self,
        command: list[str],
        roar_dir: Path,
        signal_handler: ISignalHandler,
        extra_env: dict[str, str] | None = None,
        tracer_mode_override: str | None = None,
        fallback_enabled_override: bool | None = None,
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
        mode = tracer_mode_override or self._get_tracer_mode()
        fallback_enabled = (
            fallback_enabled_override
            if fallback_enabled_override is not None
            else self._get_fallback_enabled()
        )
        candidates = self._get_tracer_candidates(mode, fallback_enabled)
        if not candidates:
            self.logger.debug("Tracer binary not found, raising error")
            if mode == "ebpf":
                hint = (
                    "roar-tracer-ebpf binary not found. Build it with:\n"
                    "  cd rust && cargo build --release -p roar-tracer-ebpf"
                    "  (eBPF; Linux; fastest but requires perms)"
                )
            elif mode == "preload":
                hint = (
                    "roar-tracer-preload or preload library not found. Build it with:\n"
                    "  cd rust && cargo build --release -p roar-tracer-preload"
                    "  (preload; macOS & Linux; no-go for statically linked libc binaries)"
                )
            elif mode == "ptrace":
                hint = (
                    "roar-tracer binary not found. Build it with:\n"
                    "  cd rust && cargo build --release -p roar-tracer"
                    "  (ptrace; Linux; slowest but broadest compatibility)"
                )
            else:
                hint = (
                    "No tracer binary found. Build one with:\n"
                    "  cd rust && cargo build --release -p roar-tracer-ebpf"
                    "  (eBPF; Linux; fastest but requires perms)\n"
                    "  cd rust && cargo build --release -p roar-tracer-preload"
                    " (preload; macOS & Linux; no-go for statically linked libc binaries)\n"
                    "  cd rust && cargo build --release -p roar-tracer"
                    "       (ptrace; Linux; slowest but broadest compatibility)"
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
        # Execute with signal handling
        self.logger.debug(
            "Installing signal handler and starting process (candidates=%s)",
            [name for name, _ in candidates],
        )
        start_time = time.time()
        signal_handler.install()

        exit_code = 1
        try:
            for idx, (backend, tracer_path) in enumerate(candidates):
                # Ensure stale files from previous attempts don't mask failures.
                for log_file in (tracer_log_file, inject_log_file):
                    try:
                        if log_file and os.path.exists(log_file):
                            os.remove(log_file)
                    except OSError:
                        pass

                tracer_cmd = [tracer_path, tracer_log_file, *command]
                self.logger.debug("Tracer command (%s): %s", backend, tracer_cmd)

                proc = None
                try:
                    attempt_env = dict(env)
                    if backend == "preload":
                        preload_lib = self._find_preload_library()
                        if preload_lib:
                            attempt_env["ROAR_PRELOAD_LIB"] = preload_lib
                    proc = subprocess.Popen(tracer_cmd, env=attempt_env)
                    self.logger.debug("Process started (%s): pid=%d", backend, proc.pid)
                    exit_code = proc.wait()
                    self.logger.debug("Process exited (%s): code=%d", backend, exit_code)
                except KeyboardInterrupt:
                    self.logger.debug("KeyboardInterrupt caught during wait (%s)", backend)
                    if proc is not None:
                        exit_code = proc.wait()
                    else:
                        exit_code = 130
                except OSError as e:
                    self.logger.warning("Failed to start %s tracer: %s", backend, e)
                    exit_code = 1

                # Tracer produced a report; no fallback needed.
                if os.path.exists(tracer_log_file):
                    break

                has_next = idx < len(candidates) - 1
                should_fallback = has_next and exit_code != 0
                if should_fallback:
                    next_backend = candidates[idx + 1][0]
                    self.logger.warning(
                        "%s tracer failed without report (exit_code=%d); falling back to %s",
                        backend,
                        exit_code,
                        next_backend,
                    )
                else:
                    break
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
