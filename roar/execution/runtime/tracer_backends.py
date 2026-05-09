"""
Shared tracer backend discovery and readiness helpers.

Centralizes backend-specific behavior so runtime selection and CLI diagnostics
use the same source of truth.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ...core.tracer_modes import TRACER_BACKEND_ORDER

EXPECTED_EBPF_CAP_NAMES = {
    "cap_bpf",
    "cap_dac_read_search",
    "cap_perfmon",
    "cap_sys_ptrace",
    "cap_sys_resource",
}

AUTO_BACKEND_ORDER = TRACER_BACKEND_ORDER
_PREFLIGHT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class TracerReadiness:
    """Typed readiness result for a concrete backend."""

    ok: bool
    reason: str | None = None

    def as_tuple(self) -> tuple[bool, str | None]:
        return self.ok, self.reason


@dataclass(frozen=True)
class BackendReadiness:
    """Typed readiness result for backend policy checks."""

    ok: bool
    detail: str

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.detail


@dataclass(frozen=True)
class PreflightCheck:
    """One backend preflight sub-check."""

    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"name": self.name, "ok": self.ok}
        if self.detail is not None:
            data["detail"] = self.detail
        return data


@dataclass(frozen=True)
class TracerPreflightResult:
    """Strict preflight result for one concrete backend."""

    backend: str
    ok: bool
    summary: str
    command_checked: str | None = None
    warnings: tuple[str, ...] = ()
    checks: tuple[PreflightCheck, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "summary": self.summary,
            "command_checked": self.command_checked,
            "warnings": list(self.warnings),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class AutoPreflightResult:
    """Strict preflight result for auto backend selection."""

    ok: bool
    selected_backend: str | None
    summary: str
    command_checked: str | None = None
    results: tuple[TracerPreflightResult, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": "auto",
            "ok": self.ok,
            "selected_backend": self.selected_backend,
            "summary": self.summary,
            "command_checked": self.command_checked,
            "results": [result.to_dict() for result in self.results],
        }


PreflightResult = TracerPreflightResult | AutoPreflightResult


def find_ptrace_tracer(package_path: Path) -> str | None:
    """Find the ptrace tracer binary."""
    return _find_binary(
        package_path=package_path,
        binary_name="roar-tracer",
    )


def find_ebpf_tracer(package_path: Path) -> str | None:
    """Find the eBPF tracer binary."""
    return _find_binary(
        package_path=package_path,
        binary_name="roar-tracer-ebpf",
    )


def find_preload_tracer(package_path: Path) -> str | None:
    """Find the preload tracer launcher binary."""
    return _find_binary(
        package_path=package_path,
        binary_name="roar-tracer-preload",
    )


def find_roard(package_path: Path) -> str | None:
    """Find the eBPF daemon binary."""
    return _find_binary(
        package_path=package_path,
        binary_name="roard",
    )


def find_proxy_binary(package_path: Path) -> str | None:
    """Find the roar-proxy binary."""
    return _find_binary(
        package_path=package_path,
        binary_name="roar-proxy",
    )


def find_preload_library(package_path: Path) -> str | None:
    """
    Find the preload interposer shared library.

    Uses platform-specific extensions:
    - Linux: .so
    - macOS: .dylib
    """
    release_dir = package_path.parent / "rust" / "target" / "release"
    deps_dir = release_dir / "deps"
    package_bin_dir = package_path / "bin"
    base_names = ("libroar_tracer_preload", "libroar-tracer-preload")
    extensions = _preload_library_extensions()

    direct_candidates: list[Path] = []
    for directory in (release_dir, package_bin_dir):
        for base_name in base_names:
            for ext in extensions:
                direct_candidates.append(directory / f"{base_name}{ext}")
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate.resolve())

    wildcard_candidates: list[Path] = []
    for directory in (release_dir, deps_dir, package_bin_dir):
        for base_name in base_names:
            for ext in extensions:
                wildcard_candidates.extend(sorted(directory.glob(f"{base_name}*{ext}")))

    for candidate in wildcard_candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def _preload_library_extensions() -> tuple[str, ...]:
    """Return preload interposer extensions accepted on this platform."""
    if sys.platform == "darwin":
        return (".dylib",)
    return (".so",)


def get_perf_event_paranoid() -> int | None:
    """Read perf_event_paranoid (Linux only)."""
    try:
        value = Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
        return int(value)
    except Exception:
        return None


def get_binary_caps(path: str) -> set[str] | None:
    """Read Linux capabilities from a binary via getcap."""
    if not shutil.which("getcap"):
        return None

    try:
        result = subprocess.run(["getcap", path], capture_output=True, text=True)
        if result.returncode != 0 or not result.stdout.strip():
            return set()
        parts = result.stdout.strip().split()
        if len(parts) < 2:
            return set()
        caps_str = parts[-1].split("=")[0]
        return {c.strip() for c in caps_str.split(",") if c.strip()}
    except Exception:
        return None


def _get_effective_cap_bitfield() -> int | None:
    """Read the effective capability bitmask from /proc/self/status.

    Returns the CapEff value as an integer, or None on non-Linux / parse failure.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return int(line.split(":")[1].strip(), 16)
    except Exception:
        return None
    return None


# Bit positions for capabilities required by the eBPF tracer.
_CAP_SYS_ADMIN = 21  # legacy fallback (kernels < 5.8)
_CAP_PERFMON = 38
_CAP_BPF = 39


def _process_has_ebpf_caps() -> bool | None:
    """Check whether the current process has effective caps for eBPF.

    Returns True/False, or None if the check is not available (non-Linux).
    """
    cap_eff = _get_effective_cap_bitfield()
    if cap_eff is None:
        return None  # cannot determine; let runtime decide
    has_bpf = bool(cap_eff & (1 << _CAP_BPF))
    has_perfmon = bool(cap_eff & (1 << _CAP_PERFMON))
    has_sys_admin = bool(cap_eff & (1 << _CAP_SYS_ADMIN))
    # Modern kernels need CAP_BPF + CAP_PERFMON; older kernels accept CAP_SYS_ADMIN.
    return (has_bpf and has_perfmon) or has_sys_admin


def ebpf_readiness(path: str) -> TracerReadiness:
    """
    Check whether eBPF tracer is likely to start.

    Returns:
        Typed readiness result for eBPF backend.
    """
    if os.geteuid() == 0:
        # Root inside a container may lack BPF capabilities.
        has_caps = _process_has_ebpf_caps()
        if has_caps is None:
            # Non-Linux or unreadable /proc; assume ready.
            return TracerReadiness(ok=True, reason=None)
        if not has_caps:
            return TracerReadiness(
                ok=False,
                reason="root lacks effective eBPF capabilities (container?)",
            )
        return TracerReadiness(ok=True, reason=None)

    paranoid = get_perf_event_paranoid()
    if paranoid is not None and paranoid > 1:
        return TracerReadiness(ok=False, reason=f"perf_event_paranoid={paranoid} (needs <= 1)")

    caps = get_binary_caps(path)
    if caps is None:
        # Unable to determine; let runtime decide.
        return TracerReadiness(ok=True, reason=None)
    if EXPECTED_EBPF_CAP_NAMES.issubset(caps):
        return TracerReadiness(ok=True, reason=None)

    missing = sorted(EXPECTED_EBPF_CAP_NAMES - caps)
    if missing:
        return TracerReadiness(ok=False, reason=f"missing capabilities: {', '.join(missing)}")
    return TracerReadiness(ok=False, reason="no capabilities set")


def ebpf_is_ready(path: str) -> tuple[bool, str | None]:
    """Backward-compatible tuple wrapper for eBPF readiness."""
    return ebpf_readiness(path).as_tuple()


def ptrace_readiness(path: str) -> TracerReadiness:
    """
    Verify the ptrace tracer binary is actually executable on this host.

    Earlier versions only checked file existence (`find_ptrace_tracer` ->
    `Path.exists()`), which silently passed when a wrong-arch binary
    shipped — e.g. an x86_64 ELF in an aarch64 wheel produces ENOEXEC at
    runtime, after `roar run` has already committed to ptrace as the
    selected backend. Now we exec the binary's `--preflight --json` and
    surface the failure up front.

    The check is `@cache`d: callers in the same process pay at most one
    subprocess invocation per binary path.
    """
    return _probe_ptrace_binary(path)


def ptrace_is_ready(path: str) -> tuple[bool, str | None]:
    """Backward-compatible tuple wrapper for ptrace readiness."""
    return ptrace_readiness(path).as_tuple()


def preload_readiness(package_path: Path, launcher_path: str | None = None) -> TracerReadiness:
    """
    Check whether preload tracer launcher + library are available.

    Returns:
        Typed readiness result for preload backend.
    """
    if not launcher_path:
        launcher_path = find_preload_tracer(package_path)
    if not launcher_path:
        return TracerReadiness(ok=False, reason="preload tracer not found")

    library_path = find_preload_library(package_path)
    if not library_path:
        return TracerReadiness(ok=False, reason="preload library not found")

    return _probe_preload_launcher(launcher_path, library_path)


def preload_is_ready(
    package_path: Path, launcher_path: str | None = None
) -> tuple[bool, str | None]:
    """Backward-compatible tuple wrapper for preload readiness."""
    return preload_readiness(package_path, launcher_path).as_tuple()


def backend_readiness(package_path: Path, backend: str) -> BackendReadiness:
    """Check readiness for backend policy: auto|ptrace|ebpf|preload."""
    if backend in ("ptrace", "ebpf", "preload"):
        return _backend_ready_non_auto(package_path, backend)

    # auto: first ready backend in preferred order
    for candidate in AUTO_BACKEND_ORDER:
        readiness = _backend_ready_non_auto(package_path, candidate)
        if readiness.ok:
            if candidate == "ptrace":
                return BackendReadiness(ok=True, detail="ptrace available")
            return BackendReadiness(ok=True, detail=f"{candidate} ready")

    return BackendReadiness(
        ok=False,
        detail="no usable tracer found (eBPF/preload not ready, ptrace not found)",
    )


def backend_ready(package_path: Path, backend: str) -> tuple[bool, str]:
    """Backward-compatible tuple wrapper for backend readiness."""
    return backend_readiness(package_path, backend).as_tuple()


def suggestions_for_preflight_result(result: PreflightResult) -> tuple[str, ...]:
    """Return concise, user-facing suggestions for a failed preflight result."""
    if result.ok:
        return ()

    if isinstance(result, AutoPreflightResult):
        suggestions: list[str] = [
            "Run `roar tracer status` to see detected tracer binaries and system readiness.",
        ]
        for backend_result in result.results:
            suggestions.extend(suggestions_for_preflight_result(backend_result))
        suggestions.append(
            "If one backend is expected to work, try `roar tracer check --backend <backend> --command <cmd>` for details."
        )
        return _dedupe_suggestions(suggestions, limit=5)

    summary = result.summary.lower()
    failed_checks = {check.name: (check.detail or "") for check in result.checks if not check.ok}
    detail_text = " ".join([summary, *failed_checks.values()]).lower()
    suggestions = [
        f"Run `roar tracer check --backend {result.backend}` to reproduce this preflight failure.",
    ]

    if result.backend == "ebpf":
        if "not found" in detail_text:
            suggestions.append(
                "Build the eBPF tracer with `cd rust && cargo build --release -p roar-tracer-ebpf`."
            )
        if "cap" in detail_text or "perf_event_paranoid" in detail_text:
            suggestions.append(
                "Run `roar tracer setup ebpf` to configure eBPF tracer capabilities and sysctls."
            )
        if "tracepoint" in detail_text or "attach" in detail_text:
            suggestions.append(
                "Try `roar tracer check --backend preload` or `--backend ptrace` on this host."
            )
        suggestions.append(
            "Use `roar run --tracer preload ...` or `roar run --tracer ptrace ...` if eBPF is not available here."
        )
    elif result.backend == "preload":
        if "not found" in detail_text or "library" in detail_text:
            suggestions.append(
                "Build the preload tracer with `cd rust && cargo build --release -p roar-tracer-preload`."
            )
        if "protected" in detail_text or "sip" in detail_text:
            suggestions.append(
                "Use a virtualenv/homebrew Python or another non-protected executable for preload tracing."
            )
        suggestions.append(
            "Use `roar run --tracer ptrace ...` on Linux if preload is blocked for this command."
        )
    elif result.backend == "ptrace":
        if "not found" in detail_text:
            suggestions.append(
                "Build the ptrace tracer with `cd rust && cargo build --release -p roar-tracer`."
            )
        if "x86_64" in detail_text or "architecture" in detail_text or "linux" in detail_text:
            suggestions.append(
                "Use `roar run --tracer preload ...` or `--tracer ebpf` on hosts unsupported by ptrace."
            )
        suggestions.append("Check kernel ptrace policy if this host restricts tracing.")

    return _dedupe_suggestions(suggestions, limit=4)


def _dedupe_suggestions(suggestions: Sequence[str], *, limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for suggestion in suggestions:
        if suggestion in seen:
            continue
        seen.add(suggestion)
        unique.append(suggestion)
        if len(unique) >= limit:
            break
    return tuple(unique)


def preflight_backend(
    package_path: Path,
    backend: str,
    command: Sequence[str] | None = None,
) -> TracerPreflightResult:
    """Run strict preflight for one concrete backend."""
    if backend == "auto":
        raise ValueError("use preflight_auto_backend() for auto policy")
    if backend == "ebpf":
        return _preflight_ebpf(package_path, command)
    if backend == "preload":
        return _preflight_preload(package_path, command)
    if backend == "ptrace":
        return _preflight_ptrace(package_path, command)
    raise ValueError(f"unknown backend: {backend}")


def preflight_auto_backend(
    package_path: Path,
    command: Sequence[str] | None = None,
) -> AutoPreflightResult:
    """Run strict preflight using normal auto backend preference order."""
    results: list[TracerPreflightResult] = []
    command_checked = _resolved_command_str(command[0]) if command else None

    for backend in AUTO_BACKEND_ORDER:
        result = preflight_backend(package_path, backend, command=command)
        results.append(result)
        command_checked = command_checked or result.command_checked
        if result.ok:
            return AutoPreflightResult(
                ok=True,
                selected_backend=backend,
                summary=f"selected backend '{backend}'",
                command_checked=command_checked,
                results=tuple(results),
            )

    return AutoPreflightResult(
        ok=False,
        selected_backend=None,
        summary="no usable tracer found",
        command_checked=command_checked,
        results=tuple(results),
    )


def _preflight_ebpf(
    package_path: Path,
    command: Sequence[str] | None = None,
) -> TracerPreflightResult:
    checks: list[PreflightCheck] = []
    ebpf = find_ebpf_tracer(package_path)
    if not ebpf:
        return TracerPreflightResult(
            backend="ebpf",
            ok=False,
            summary="eBPF tracer not found",
            command_checked=_resolved_command_str(command[0]) if command else None,
            checks=(PreflightCheck("binary", False, "roar-tracer-ebpf not found"),),
        )

    checks.append(PreflightCheck("binary", True, ebpf))
    readiness = ebpf_readiness(ebpf)
    checks.append(PreflightCheck("static_readiness", readiness.ok, readiness.reason or "ready"))
    if not readiness.ok:
        return TracerPreflightResult(
            backend="ebpf",
            ok=False,
            summary=readiness.reason or "eBPF preflight failed",
            command_checked=_resolved_command_str(command[0]) if command else None,
            checks=tuple(checks),
        )

    result = _run_json_binary_preflight((ebpf, "--preflight", "--json"), backend="ebpf")
    return _merge_preflight_checks(result, checks)


def _preflight_preload(
    package_path: Path,
    command: Sequence[str] | None = None,
) -> TracerPreflightResult:
    checks: list[PreflightCheck] = []
    launcher = find_preload_tracer(package_path)
    if not launcher:
        return TracerPreflightResult(
            backend="preload",
            ok=False,
            summary="preload tracer not found",
            checks=(PreflightCheck("launcher", False, "roar-tracer-preload not found"),),
        )
    checks.append(PreflightCheck("launcher", True, launcher))

    preflight_command: list[str] = [launcher, "--preflight", "--json"]
    if command:
        preflight_command.extend(["--command", command[0]])

    env_overrides: dict[str, str] | None = None
    library = find_preload_library(package_path)
    if library:
        env_overrides = {"ROAR_PRELOAD_LIB": library}

    result = _run_json_binary_preflight(
        preflight_command,
        backend="preload",
        env_overrides=env_overrides,
    )
    return _merge_preflight_checks(result, checks)


def _preflight_ptrace(
    package_path: Path,
    command: Sequence[str] | None = None,
) -> TracerPreflightResult:
    checks: list[PreflightCheck] = []
    ptrace = find_ptrace_tracer(package_path)
    if not ptrace:
        return TracerPreflightResult(
            backend="ptrace",
            ok=False,
            summary="ptrace tracer not found",
            command_checked=_resolved_command_str(command[0]) if command else None,
            checks=(PreflightCheck("binary", False, "roar-tracer not found"),),
        )
    checks.append(PreflightCheck("binary", True, ptrace))

    preflight_command: list[str] = [ptrace, "--preflight", "--json"]
    if command:
        preflight_command.extend(["--command", command[0]])

    result = _run_json_binary_preflight(preflight_command, backend="ptrace")
    return _merge_preflight_checks(result, checks)


def _backend_ready_non_auto(package_path: Path, backend: str) -> BackendReadiness:
    if backend == "ptrace":
        ptrace = find_ptrace_tracer(package_path)
        if not ptrace:
            return BackendReadiness(ok=False, detail="ptrace tracer not found")
        ok, reason = ptrace_is_ready(ptrace)
        return BackendReadiness(ok=ok, detail=reason or ptrace)

    if backend == "ebpf":
        ebpf = find_ebpf_tracer(package_path)
        if not ebpf:
            return BackendReadiness(ok=False, detail="eBPF tracer not found")
        ok, reason = ebpf_is_ready(ebpf)
        return BackendReadiness(ok=ok, detail=reason or "ready")

    preload = find_preload_tracer(package_path)
    if not preload:
        return BackendReadiness(ok=False, detail="preload tracer not found")
    ok, reason = preload_is_ready(package_path, preload)
    return BackendReadiness(ok=ok, detail=reason or "ready")


def _find_binary(package_path: Path, binary_name: str) -> str | None:
    """Find a tracer-related executable in dev, package, or PATH locations."""
    candidates = [
        package_path.parent / "rust" / "target" / "release" / binary_name,
        package_path / "bin" / binary_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())

    resolved = shutil.which(binary_name)
    return resolved if resolved else None


def _merge_preflight_checks(
    result: TracerPreflightResult,
    checks: Sequence[PreflightCheck],
) -> TracerPreflightResult:
    return TracerPreflightResult(
        backend=result.backend,
        ok=result.ok,
        summary=result.summary,
        command_checked=result.command_checked,
        warnings=result.warnings,
        checks=(*checks, *result.checks),
    )


def _run_json_binary_preflight(
    command: Sequence[str],
    *,
    backend: str,
    env_overrides: dict[str, str] | None = None,
) -> TracerPreflightResult:
    try:
        env = dict(os.environ)
        if env_overrides:
            env.update(env_overrides)
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except OSError as exc:
        return TracerPreflightResult(
            backend=backend,
            ok=False,
            summary=f"failed to exec preflight: {exc}",
            checks=(PreflightCheck("binary_preflight", False, str(exc)),),
        )
    except subprocess.TimeoutExpired:
        return TracerPreflightResult(
            backend=backend,
            ok=False,
            summary="preflight timed out",
            checks=(PreflightCheck("binary_preflight", False, "timed out"),),
        )

    payload = _parse_json_payload(result.stdout)
    if payload is not None:
        preflight = _preflight_result_from_payload(backend, payload)
        if not preflight.ok and result.returncode == 0 and not preflight.summary:
            return TracerPreflightResult(
                backend=backend,
                ok=False,
                summary="preflight failed",
                checks=preflight.checks,
                warnings=preflight.warnings,
                command_checked=preflight.command_checked,
            )
        return preflight

    detail = _first_nonempty_line(result.stderr) or _first_nonempty_line(result.stdout)
    if result.returncode != 0:
        return TracerPreflightResult(
            backend=backend,
            ok=False,
            summary=detail or f"preflight failed with exit code {result.returncode}",
            checks=(
                PreflightCheck(
                    "binary_preflight",
                    False,
                    detail or f"exit code {result.returncode}",
                ),
            ),
        )

    return TracerPreflightResult(
        backend=backend,
        ok=False,
        summary="preflight produced no JSON output",
        checks=(PreflightCheck("binary_preflight", False, "no JSON output"),),
    )


@cache
def _probe_ptrace_binary(path: str) -> TracerReadiness:
    """Execute `<binary> --preflight --json` and surface exec/probe failures.

    Catches the wrong-arch / not-an-ELF case (subprocess raises OSError
    with errno=ENOEXEC), which the existence-only check used to pass.
    """
    try:
        result = subprocess.run(
            [path, "--preflight", "--json"],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        return TracerReadiness(ok=False, reason=f"ptrace tracer failed to exec: {exc}")
    except subprocess.TimeoutExpired:
        return TracerReadiness(ok=False, reason="ptrace tracer probe timed out")

    payload = _parse_json_payload(result.stdout)
    if payload is not None:
        if payload.get("ok") is True:
            return TracerReadiness(ok=True, reason=None)
        summary = payload.get("summary")
        if isinstance(summary, str) and summary:
            return TracerReadiness(ok=False, reason=summary)
        return TracerReadiness(ok=False, reason="ptrace preflight failed")

    detail = _first_nonempty_line(result.stderr) or _first_nonempty_line(result.stdout)
    if result.returncode != 0:
        return TracerReadiness(
            ok=False,
            reason=detail or f"ptrace preflight failed with exit code {result.returncode}",
        )
    return TracerReadiness(ok=False, reason="ptrace preflight produced no JSON output")


@cache
def _probe_preload_launcher(
    launcher_path: str,
    library_path: str,
    probe_command: tuple[str, ...] | None = None,
) -> TracerReadiness:
    env = dict(os.environ)
    env["ROAR_PRELOAD_LIB"] = library_path

    if probe_command is None:
        probe_command = (sys.executable, "-c", "pass")

    with tempfile.TemporaryDirectory(prefix="roar-preload-check-") as tmp_dir:
        report_path = Path(tmp_dir) / "probe.json"
        command = [launcher_path, str(report_path), *probe_command]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_PREFLIGHT_TIMEOUT_SECONDS,
                check=False,
                env=env,
            )
        except OSError as exc:
            return TracerReadiness(ok=False, reason=f"preload launcher failed to exec: {exc}")
        except subprocess.TimeoutExpired:
            return TracerReadiness(ok=False, reason="preload launcher probe timed out")

        if result.returncode != 0:
            detail = _first_nonempty_line(result.stderr) or _first_nonempty_line(result.stdout)
            if detail:
                return TracerReadiness(ok=False, reason=f"preload launcher probe failed: {detail}")
            return TracerReadiness(
                ok=False,
                reason=f"preload launcher probe failed with exit code {result.returncode}",
            )

        if not report_path.exists():
            return TracerReadiness(ok=False, reason="preload launcher probe produced no report")

    return TracerReadiness(ok=True, reason=None)


def _resolved_command_str(command: str) -> str | None:
    resolved = _resolve_command_path(command)
    if resolved is not None:
        return str(resolved)
    return command


def _resolve_command_path(command: str) -> Path | None:
    command_path = Path(command)
    if command_path.is_absolute() or "/" in command:
        return command_path.resolve() if command_path.exists() else None

    resolved = shutil.which(command)
    if not resolved:
        return None
    return Path(resolved).resolve()


def _parse_json_payload(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _preflight_result_from_payload(
    backend: str,
    payload: dict[str, object],
) -> TracerPreflightResult:
    parsed_backend = payload.get("backend")
    parsed_ok = payload.get("ok")
    parsed_summary = payload.get("summary")
    parsed_command_checked = payload.get("command_checked")
    parsed_warnings = payload.get("warnings")
    parsed_checks = payload.get("checks")

    warnings: list[str] = []
    if isinstance(parsed_warnings, list):
        for warning in parsed_warnings:
            if isinstance(warning, str):
                warnings.append(warning)

    checks: list[PreflightCheck] = []
    if isinstance(parsed_checks, list):
        for item in parsed_checks:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            ok = item.get("ok")
            detail = item.get("detail")
            if isinstance(name, str) and isinstance(ok, bool):
                checks.append(
                    PreflightCheck(
                        name=name,
                        ok=ok,
                        detail=detail if isinstance(detail, str) else None,
                    )
                )

    return TracerPreflightResult(
        backend=parsed_backend if isinstance(parsed_backend, str) else backend,
        ok=parsed_ok if isinstance(parsed_ok, bool) else False,
        summary=parsed_summary if isinstance(parsed_summary, str) else "preflight failed",
        command_checked=(
            parsed_command_checked if isinstance(parsed_command_checked, str) else None
        ),
        warnings=tuple(warnings),
        checks=tuple(checks),
    )


def _first_nonempty_line(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line:
            return line[:200]
    return None
