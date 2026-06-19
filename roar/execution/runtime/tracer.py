"""
Tracer service for process execution with file I/O tracking.

Handles tracer binary discovery and process execution via the tracer.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ...core.exceptions import (
    CommandNotFoundError,
    TracerNotFoundError,
    TracerPreflightError,
)
from ...core.interfaces.logger import ILogger
from ...core.interfaces.run import ISignalHandler
from ...core.models.run import TracerResult
from ...core.tracer_modes import TRACER_BACKEND_ORDER, is_valid_tracer_mode
from ...execution.runtime import tracer_backends


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
        # Go up 3 levels: runtime -> execution -> roar
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
        """Get the configured tracer mode (auto, ebpf, preload, ptrace)."""
        try:
            from ...integrations.config import config_get

            mode = config_get("tracer.default")
            if isinstance(mode, str) and is_valid_tracer_mode(mode):
                return mode
        except Exception:
            pass
        return "auto"

    def _get_fallback_enabled(self) -> bool:
        """Get whether tracer fallback is enabled."""
        try:
            from ...integrations.config import config_get

            value = config_get("tracer.fallback_enabled")
            if isinstance(value, bool):
                return value
        except Exception:
            pass
        return True

    def _runtime_pythonpath_entries(self) -> list[str]:
        """Return Roar runtime import roots for injected child interpreters."""
        entries: list[str] = []
        seen: set[str] = set()

        def add(path: str | Path) -> None:
            resolved = str(Path(path).resolve())
            if resolved in seen or not os.path.exists(resolved):
                return
            seen.add(resolved)
            entries.append(resolved)

        # The installed package root, or the source checkout root for editable installs.
        add(Path(__file__).resolve().parents[3])

        # Editable installs keep dependencies in the parent interpreter's site-packages.
        for path in sys.path:
            if not path:
                continue
            if "site-packages" in path or "dist-packages" in path:
                add(path)

        return entries

    def _lazy_install_runtime_entries(self, command: list[str], roar_dir: Path) -> list[str]:
        """Probe the target Python and lazy-install a matching runtime tree on mismatch.

        Returns a list of site-packages paths to prepend to
        ``ROAR_RUNTIME_PYTHONPATH``. Empty on:
        - non-Python targets (bash, make, etc.) — can't probe a python ABI;
        - matching ABI — bundled deps work as-is;
        - ``runtime.install = skip`` — opted out;
        - install failure (no network, no installer, etc.) — the sitecustomize
          gate handles the fallback.
        """
        if not command:
            return []

        # Resolve to an absolute interpreter path up front so the ABI probe and the
        # `uv pip install --python` target agree. A bare name like "python" is resolved
        # here against PATH (the env the run executes in), but `uv --python python` uses
        # uv's OWN discovery — which can pick a different interpreter (e.g. a 3.13 venv),
        # installing wrong-ABI wheels into the runtime tree. Pinning the absolute path
        # keeps the probe and the install on the same interpreter.
        try:
            target_python = shutil.which(command[0]) or command[0]
        except OSError:
            target_python = command[0]

        # Fast path: if the target Python is the same executable roar-cli itself runs
        # under, the ABI matches by construction — skip the probe subprocess entirely.
        if os.path.realpath(target_python) == os.path.realpath(sys.executable):
            return []

        try:
            from roar import __version__ as roar_version

            from .abi_probe import probe_python_abi
            from .lazy_install import ensure_runtime
        except ImportError as exc:
            # An import failure here is an internal contract violation (the
            # imported names should always be available in a normal install),
            # not a user-environment thing — log loud enough that a corrupted
            # install surfaces in --verbose runs.
            self.logger.warning("lazy-install init: import failed: %s", exc)
            return []

        target_abi = probe_python_abi(target_python)
        if not target_abi:
            return []
        bundled_abi = sys.implementation.cache_tag
        if target_abi == bundled_abi:
            return []
        try:
            tree = ensure_runtime(
                target_python=target_python,
                target_abi=target_abi,
                bundled_abi=bundled_abi,
                roar_version=roar_version,
                start_dir=roar_dir,
            )
        except Exception as exc:
            self.logger.debug("lazy-install failed: %s", exc)
            return []
        if tree is None:
            return []
        return [str(tree)]

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

    def _ptrace_is_ready(self, path: str) -> tuple[bool, str | None]:
        """Check whether the ptrace tracer binary is exec'able and its
        preflight passes. Catches the wrong-arch ENOEXEC case that the
        old existence-only check let through."""
        return tracer_backends.ptrace_readiness(path).as_tuple()

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
                return None
            if require_ready:
                ready, reason = self._preload_is_ready(preload)
                if not ready:
                    self.logger.debug("Skipping preload tracer in auto mode: %s", reason)
                    return None
            return "preload", preload

        if backend == "ptrace":
            ptrace = self._find_ptrace_tracer()
            if not ptrace:
                return None
            if require_ready:
                ready, reason = self._ptrace_is_ready(ptrace)
                if not ready:
                    self.logger.debug("Skipping ptrace tracer in auto mode: %s", reason)
                    return None
            return "ptrace", ptrace
        return None

    def _build_tracer_not_found_hint(self, mode: str) -> str:
        """Build the user-facing tracer-not-found hint for a mode.

        Almost everyone who hits this installed roar from PyPI, where the wheel
        is supposed to bundle the tracer binary — so the real fix is a
        reinstall, not a Rust build. Lead with that. The ``cargo build`` path
        only applies to people working from a source checkout or an sdist
        install (no published wheel for their platform), so it's labelled as
        such rather than presented as the headline instruction — the previous
        message told every user to ``cd rust && cargo build`` against a
        directory that doesn't exist in their project.
        """
        what = {
            "ebpf": "roar-tracer-ebpf binary",
            "preload": "roar-tracer-preload / preload library",
            "ptrace": "roar-tracer binary",
        }.get(mode, "tracer binary")
        crates = {
            "ebpf": ["roar-tracer-ebpf"],
            "preload": ["roar-tracer-preload"],
            "ptrace": ["roar-tracer"],
        }.get(mode, ["roar-tracer-ebpf", "roar-tracer-preload", "roar-tracer"])
        build_lines = "\n".join(f"      cargo build --release -p {crate}" for crate in crates)
        return (
            f"No {what} found — this roar install is missing its tracer backend.\n"
            "  - Installed from PyPI (uv tool / pip)? The wheel ships the tracer, "
            "so this is a broken install. Reinstall it:\n"
            "      uv tool install --reinstall roar-cli   "
            "(or: pip install --force-reinstall roar-cli)\n"
            "    and please report it at https://github.com/treqs/roar/issues\n"
            "  - Working from a roar source checkout or sdist? Build the tracer "
            "from the rust/ tree:\n"
            "      cd rust\n"
            f"{build_lines}"
        )

    def _preflight_candidate(
        self,
        backend: str,
        command: list[str],
    ) -> tracer_backends.TracerPreflightResult:
        """Run strict preflight for one concrete backend."""
        return tracer_backends.preflight_backend(self._package_path, backend, command=command)

    def resolve_execution_candidates(
        self,
        command: list[str],
        tracer_mode_override: str | None = None,
        fallback_enabled_override: bool | None = None,
    ) -> list[tuple[str, str]]:
        """Resolve concrete tracer candidates that passed strict preflight."""
        mode = tracer_mode_override or self._get_tracer_mode()
        fallback_enabled = (
            fallback_enabled_override
            if fallback_enabled_override is not None
            else self._get_fallback_enabled()
        )
        self.logger.debug(
            "Resolving execution candidates: mode=%s fallback_enabled=%s command=%s",
            mode,
            fallback_enabled,
            command,
        )

        candidates = self._get_tracer_candidates(mode, fallback_enabled)
        if not candidates:
            raise TracerNotFoundError(self._build_tracer_not_found_hint(mode))

        approved: list[tuple[str, str]] = []
        failures: list[tracer_backends.TracerPreflightResult] = []
        for backend, tracer_path in candidates:
            result = self._preflight_candidate(backend, command)
            if result.ok:
                approved.append((backend, tracer_path))
                continue

            failures.append(result)
            self.logger.debug(
                "Skipping %s tracer after failed preflight: %s", backend, result.summary
            )

        if approved:
            if not fallback_enabled:
                return [approved[0]]
            return approved

        # If any backend got far enough to determine the command itself is
        # missing, that's the real problem — not the tracers. Surface it as a
        # clean "command not found" instead of tracer-build instructions.
        if command and any(
            tracer_backends.preflight_failed_on_missing_command(result) for result in failures
        ):
            raise CommandNotFoundError(command[0])

        failure_context = {"failures": [result.to_dict() for result in failures]}
        if mode == "auto":
            detail = "; ".join(f"{result.backend}: {result.summary}" for result in failures)
            raise TracerPreflightError(
                self._format_preflight_failure_message(
                    "auto",
                    failures,
                    summary=f"No usable tracer passed preflight: {detail or 'no usable tracer found'}",
                ),
                backend="auto",
                context=failure_context,
            )

        summary = failures[0].summary if failures else "preflight failed"
        raise TracerPreflightError(
            self._format_preflight_failure_message(
                mode,
                failures,
                summary=f"Tracer preflight failed for '{mode}': {summary}",
            ),
            backend=mode,
            context=failure_context,
        )

    def _format_preflight_failure_message(
        self,
        mode: str,
        failures: list[tracer_backends.TracerPreflightResult],
        *,
        summary: str,
    ) -> str:
        """Build a concise user-facing preflight error with next steps."""
        if mode == "auto":
            result: tracer_backends.PreflightResult = tracer_backends.AutoPreflightResult(
                ok=False,
                selected_backend=None,
                summary=summary,
                results=tuple(failures),
            )
        elif failures:
            result = failures[0]
        else:
            return summary

        suggestions = tracer_backends.suggestions_for_preflight_result(result)
        if not suggestions:
            return summary

        lines = [summary, "", "Next steps:"]
        lines.extend(f"  - {suggestion}" for suggestion in suggestions)
        return "\n".join(lines)

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
        job_id: str | None = None,
        tracer_mode_override: str | None = None,
        fallback_enabled_override: bool | None = None,
        candidates_override: list[tuple[str, str]] | None = None,
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
        candidates = candidates_override or self.resolve_execution_candidates(
            command,
            tracer_mode_override=mode,
            fallback_enabled_override=fallback_enabled,
        )

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
            from ...integrations.config import load_config

            config = load_config()
            config_env = config.get("env", {})
            if isinstance(config_env, dict):
                env.update(config_env)
        except Exception:
            pass  # Best-effort

        # Merge extra env (e.g. AWS_ENDPOINT_URL from proxy)
        if extra_env:
            env.update(extra_env)

        # Make sitecustomize discoverable while letting the workload's own venv
        # keep precedence over Roar's runtime dependencies.
        inject_dir = str(Path(__file__).resolve().parent / "inject")
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{inject_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else inject_dir
        )
        runtime_entries = self._lazy_install_runtime_entries(command, roar_dir)
        runtime_entries.extend(self._runtime_pythonpath_entries())
        env["ROAR_RUNTIME_PYTHONPATH"] = os.pathsep.join(runtime_entries)
        env["ROAR_LOG_FILE"] = inject_log_file
        env["ROAR_WRAP"] = "1"
        env["ROAR_PROJECT_DIR"] = str(roar_dir.parent)
        resolved_job_id = job_id or env.get("ROAR_JOB_ID")
        if resolved_job_id:
            env["ROAR_JOB_ID"] = resolved_job_id

        # Build tracer command
        # Execute with signal handling
        self.logger.debug(
            "Installing signal handler and starting process (candidates=%s)",
            [name for name, _ in candidates],
        )
        start_time = time.time()
        signal_handler.install()

        exit_code = 1
        selected_backend: str | None = None
        try:
            for idx, (backend, tracer_path) in enumerate(candidates):
                selected_backend = backend
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
            backend=selected_backend,
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
