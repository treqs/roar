"""Regression coverage for dup2/dup3 fd→path propagation in the ptrace tracer.

Without dup tracking, `bash -c "echo hi > out"` is missed entirely: bash opens
the redirect target on a fresh fd, dup3's it onto stdout (1), and writes via
fd 1. The ptrace tracer recorded the open but couldn't attribute the write
back to the file path.

These tests exercise the syscall path directly (so they fail loudly if the
seccomp filter or the dup handler regress) and assert the failed-dup safety
invariant separately from the success path.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

try:
    import msgpack  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - dev dep
    msgpack = None

import pytest

import tests.conftest as test_conftest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="ptrace tracer requires Linux"),
    pytest.mark.skipif(msgpack is None, reason="msgpack not installed"),
]


def _ensure_ptrace_tracer() -> Path:
    test_conftest._ensure_repo_local_ptrace_tracer()
    return test_conftest.RELEASE_BIN_DIR / "roar-tracer"


def _run_under_tracer(tracer: Path, workload: list[str], *, cwd: Path) -> dict:
    report = cwd / "report.msgpack"
    proc = subprocess.run(
        [str(tracer), str(report), *workload],
        cwd=str(cwd),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"tracer exited {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert report.exists() and report.stat().st_size > 0, "no report produced"
    return msgpack.unpackb(report.read_bytes(), raw=False)


def _written(report: dict) -> set[str]:
    paths = set(report.get("written_files") or [])
    for entry in report.get("files") or []:
        if entry.get("written"):
            paths.add(entry["path"])
    return paths


def test_bash_redirect_recorded_as_write(tmp_path: Path) -> None:
    """`bash -c 'echo > x'` uses dup3 to alias fd→stdout. Without dup
    handling the write was attributed to "unknown fd" and dropped."""
    tracer = _ensure_ptrace_tracer()
    target = tmp_path / "redirect_target.txt"

    report = _run_under_tracer(
        tracer,
        ["bash", "-c", f"echo hi > {target}"],
        cwd=tmp_path,
    )

    written = _written(report)
    assert str(target.resolve()) in written, (
        f"expected redirect target in written_files, got: {sorted(written)}"
    )
    assert target.exists() and target.read_text().strip() == "hi"


def test_failed_dup3_does_not_corrupt_existing_fd_mapping(tmp_path: Path) -> None:
    """A failed dup3 must NOT call handle_dup — otherwise the destination
    fd's existing path mapping would be replaced with the (untracked or
    invalid) source's path, and subsequent writes would be lost."""
    tracer = _ensure_ptrace_tracer()
    target = tmp_path / "dst.txt"

    workload_src = tmp_path / "workload.py"
    workload_src.write_text(
        """
import ctypes, os, sys
dst = sys.argv[1]
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
os.dup2(fd, 100)
os.close(fd)
os.write(100, b"first ")  # fd 100 -> dst (legitimate)

# Failed dup3 with bogus old_fd. Bypass glibc to avoid any retry magic.
libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_dup3 = 24 if os.uname().machine == "aarch64" else 292
ret = libc.syscall(SYS_dup3, 9999, 100, 0)
if ret != -1:
    raise SystemExit(f"expected dup3 failure, got {ret}")

# fd 100 must STILL map to dst per POSIX (oldfd invalid → newfd not closed).
os.write(100, b"second")
os.close(100)
""".lstrip(),
    )

    report = _run_under_tracer(
        tracer,
        [sys.executable, str(workload_src), str(target)],
        cwd=tmp_path,
    )

    # Both writes (before AND after the failed dup3) must be attributed to
    # dst. If the tracer wrongly applied handle_dup on failure, the second
    # write would be attributed to fd 9999's (empty) path and dropped.
    assert target.exists() and target.read_text() == "first second"
    written = _written(report)
    assert str(target.resolve()) in written, (
        f"expected dst in written_files even after failed dup3 — got: {sorted(written)}"
    )


def test_python_workload_still_captured(tmp_path: Path) -> None:
    """Regression check: the existing single-process python flow that
    doesn't use dup must still be tracked (ensures we didn't accidentally
    break the read/write capture path)."""
    tracer = _ensure_ptrace_tracer()
    src = tmp_path / "input.txt"
    dst = tmp_path / "output.txt"
    src.write_bytes(b"data\n")

    workload_src = tmp_path / "workload.py"
    workload_src.write_text(
        "import sys; open(sys.argv[2],'wb').write(b'pre:'+open(sys.argv[1],'rb').read())\n"
    )
    report = _run_under_tracer(
        tracer,
        [sys.executable, str(workload_src), str(src), str(dst)],
        cwd=tmp_path,
    )

    written = _written(report)
    read = set(report.get("read_files") or [])
    for entry in report.get("files") or []:
        if entry.get("read"):
            read.add(entry["path"])

    assert str(src.resolve()) in read
    assert str(dst.resolve()) in written
