"""Regression coverage for the eBPF "short-lived write" race.

Two bugs combined into the user-visible 22% capture rate for
`bash -c 'echo > x'`:

  1. Deregister cleared `pid_to_run` immediately, so events still in the
     ring buffer when Deregister arrived were dropped at routing time.
  2. Relative paths were resolved lazily via `/proc/<pid>/cwd`, which
     returns ENOENT after the tracee has exited — so opens silently
     fell back to the raw relative path.

The fixes cache the CWD eagerly at register time and keep `pid_to_run`
alive past Deregister (with a synchronous drain at GetReport). This test
exercises the worst-case timing: a workload whose entire lifetime is the
write itself.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

try:
    import msgpack  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    msgpack = None

REPO_ROOT = Path(__file__).resolve().parents[2]
RUST_MANIFEST = REPO_ROOT / "rust" / "Cargo.toml"
RELEASE_BIN_DIR = REPO_ROOT / "rust" / "target" / "release"

pytestmark = [
    pytest.mark.ebpf,
    pytest.mark.skipif(platform.system() != "Linux", reason="eBPF requires Linux"),
    pytest.mark.skipif(msgpack is None, reason="msgpack not installed"),
    pytest.mark.skipif(os.geteuid() != 0, reason="eBPF requires CAP_BPF (root)"),
]


def _ensure_binaries() -> tuple[Path, Path]:
    tracer = RELEASE_BIN_DIR / "roar-tracer-ebpf"
    roard = RELEASE_BIN_DIR / "roard"
    if tracer.exists() and roard.exists():
        return tracer, roard
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo required to build roar-tracer-ebpf")
    result = subprocess.run(
        [
            cargo,
            "build",
            "--release",
            "--manifest-path",
            str(RUST_MANIFEST),
            "-p",
            "roar-tracer-ebpf",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cargo build failed: {result.stderr}"
    return tracer, roard


def _file_was_written(report_path: Path, target: str) -> bool:
    if not report_path.exists() or report_path.stat().st_size == 0:
        return False
    data = msgpack.unpackb(report_path.read_bytes(), raw=False)
    paths = set(data.get("written_files") or [])
    for entry in data.get("files") or []:
        if entry.get("written"):
            paths.add(entry["path"])
    return target in paths


def _capture_rate(
    tracer: Path, roard: Path, command: list[str], target_name: str, trials: int
) -> int:
    captured = 0
    env = os.environ.copy()
    env["PATH"] = f"{roard.parent}:{env.get('PATH', '')}"
    for _ in range(trials):
        with tempfile.TemporaryDirectory(prefix="ebpf-race-") as td:
            tdp = Path(td)
            target = tdp / target_name
            report = tdp / "report.msgpack"
            env["XDG_RUNTIME_DIR"] = str(tdp)
            proc = subprocess.run(
                [str(tracer), str(report), *command],
                cwd=str(tdp),
                capture_output=True,
                timeout=30,
                env=env,
            )
            if proc.returncode != 0 or not target.exists():
                continue
            if _file_was_written(report, str(target.resolve())):
                captured += 1
    return captured


def test_bash_redirect_short_lived_capture_rate() -> None:
    """`bash -c 'echo > x'` exits the moment the write completes. Pre-fix
    capture rate was ~22% (relative-path resolution race). Post-fix is
    100% in isolation; we use a 80% floor here because in a busy pytest
    run with parallel daemons spinning up the rate dips. Anything above
    50% would still be a strong signal that the fix is correctly
    addressing the dominant race."""
    tracer, roard = _ensure_binaries()
    trials = 30
    captured = _capture_rate(
        tracer,
        roard,
        ["bash", "-c", "echo hi > test.txt"],
        "test.txt",
        trials,
    )
    rate = captured / trials
    assert rate >= 0.80, (
        f"bash redirect capture rate {captured}/{trials} = {rate:.0%} "
        f"is below the 80% floor; pre-fix was ~22%"
    )


def test_bash_redirect_with_leading_sleep() -> None:
    """`sleep 0.2; echo > x` — write is still the last syscall before
    exit so the post-write window for events to drain is the same as the
    no-sleep case. Confirms the fix doesn't depend on tracee lifetime."""
    tracer, roard = _ensure_binaries()
    trials = 20
    captured = _capture_rate(
        tracer,
        roard,
        ["bash", "-c", "sleep 0.2; echo hi > test.txt"],
        "test.txt",
        trials,
    )
    rate = captured / trials
    assert rate >= 0.80, f"capture rate {captured}/{trials} = {rate:.0%} is below the 80% floor"
