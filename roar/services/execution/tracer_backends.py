"""
Shared tracer backend discovery and readiness helpers.

Centralizes backend-specific behavior so runtime selection and CLI diagnostics
use the same source of truth.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
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
    return TracerReadiness(ok=True, reason=None)


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


def _backend_ready_non_auto(package_path: Path, backend: str) -> BackendReadiness:
    if backend == "ptrace":
        ptrace = find_ptrace_tracer(package_path)
        return (
            BackendReadiness(ok=True, detail=ptrace)
            if ptrace
            else BackendReadiness(ok=False, detail="ptrace tracer not found")
        )

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
