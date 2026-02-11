"""
Shared tracer backend discovery and readiness helpers.

Centralizes backend-specific behavior so runtime selection and CLI diagnostics
use the same source of truth.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

EXPECTED_EBPF_CAP_NAMES = {
    "cap_bpf",
    "cap_dac_read_search",
    "cap_perfmon",
    "cap_sys_ptrace",
    "cap_sys_resource",
}

AUTO_BACKEND_ORDER = ("ebpf", "preload", "ptrace")


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
    """Find the LD_PRELOAD tracer launcher binary."""
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


def find_preload_library(package_path: Path) -> str | None:
    """
    Find the preload interposer shared library.

    Preferred names:
    - libroar_tracer_preload.so
    - libroar-tracer-preload.so
    """
    release_dir = package_path.parent / "rust" / "target" / "release"
    deps_dir = release_dir / "deps"
    package_bin_dir = package_path / "bin"

    direct_candidates = [
        release_dir / "libroar_tracer_preload.so",
        release_dir / "libroar-tracer-preload.so",
        package_bin_dir / "libroar_tracer_preload.so",
        package_bin_dir / "libroar-tracer-preload.so",
    ]
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate.resolve())

    wildcard_candidates: list[Path] = []
    wildcard_candidates.extend(sorted(release_dir.glob("libroar_tracer_preload*.so")))
    wildcard_candidates.extend(sorted(release_dir.glob("libroar-tracer-preload*.so")))
    wildcard_candidates.extend(sorted(deps_dir.glob("libroar_tracer_preload*.so")))
    wildcard_candidates.extend(sorted(deps_dir.glob("libroar-tracer-preload*.so")))
    wildcard_candidates.extend(sorted(package_bin_dir.glob("libroar_tracer_preload*.so")))
    wildcard_candidates.extend(sorted(package_bin_dir.glob("libroar-tracer-preload*.so")))

    for candidate in wildcard_candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


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


def ebpf_is_ready(path: str) -> tuple[bool, str | None]:
    """
    Check whether eBPF tracer is likely to start.

    Returns:
        (is_ready, reason_if_not_ready)
    """
    if os.geteuid() == 0:
        return True, None

    paranoid = get_perf_event_paranoid()
    if paranoid is not None and paranoid > 1:
        return False, f"perf_event_paranoid={paranoid} (needs <= 1)"

    caps = get_binary_caps(path)
    if caps is None:
        # Unable to determine; let runtime decide.
        return True, None
    if EXPECTED_EBPF_CAP_NAMES.issubset(caps):
        return True, None

    missing = sorted(EXPECTED_EBPF_CAP_NAMES - caps)
    if missing:
        return False, f"missing capabilities: {', '.join(missing)}"
    return False, "no capabilities set"


def preload_is_ready(
    package_path: Path, launcher_path: str | None = None
) -> tuple[bool, str | None]:
    """
    Check whether preload tracer launcher + library are available.

    Returns:
        (is_ready, reason_if_not_ready)
    """
    if not launcher_path:
        launcher_path = find_preload_tracer(package_path)
    if not launcher_path:
        return False, "preload tracer not found"

    library_path = find_preload_library(package_path)
    if not library_path:
        return False, "preload library not found"
    return True, None


def backend_ready(package_path: Path, backend: str) -> tuple[bool, str]:
    """Check readiness for backend policy: auto|ptrace|ebpf|preload."""
    if backend == "ptrace":
        ptrace = find_ptrace_tracer(package_path)
        return (True, ptrace) if ptrace else (False, "ptrace tracer not found")

    if backend == "ebpf":
        ebpf = find_ebpf_tracer(package_path)
        if not ebpf:
            return False, "eBPF tracer not found"
        ok, reason = ebpf_is_ready(ebpf)
        return ok, reason or "ready"

    if backend == "preload":
        preload = find_preload_tracer(package_path)
        if not preload:
            return False, "preload tracer not found"
        ok, reason = preload_is_ready(package_path, preload)
        return ok, reason or "ready"

    # auto: first ready backend in preferred order
    ebpf = find_ebpf_tracer(package_path)
    if ebpf:
        ok, _ = ebpf_is_ready(ebpf)
        if ok:
            return True, "eBPF ready"

    preload = find_preload_tracer(package_path)
    if preload:
        ok, _ = preload_is_ready(package_path, preload)
        if ok:
            return True, "preload ready"

    ptrace = find_ptrace_tracer(package_path)
    if ptrace:
        return True, "ptrace available"

    return False, "no usable tracer found (eBPF/preload not ready, ptrace not found)"


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
