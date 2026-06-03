"""Regression coverage for ptrace tracking of splice(2) reads."""

from __future__ import annotations

import os
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
    pytest.mark.skipif(not hasattr(os, "splice"), reason="Python lacks os.splice"),
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


def _read(report: dict) -> set[str]:
    paths = set(report.get("read_files") or [])
    for entry in report.get("files") or []:
        if entry.get("read"):
            paths.add(entry["path"])
    return paths


def test_splice_file_to_pipe_records_source_as_read(tmp_path: Path) -> None:
    """A file->pipe splice moves bytes from the file without read(2)."""
    tracer = _ensure_ptrace_tracer()
    src = tmp_path / "input.txt"
    src.write_bytes(b"hello via splice")

    workload_src = tmp_path / "splice_workload.py"
    workload_src.write_text(
        """
import os
import sys

src = sys.argv[1]
fd = os.open(src, os.O_RDONLY)
read_fd, write_fd = os.pipe()
try:
    bytes_moved = os.splice(fd, write_fd, 1024)
    os.close(write_fd)
    write_fd = -1
    payload = os.read(read_fd, 1024)
finally:
    for candidate in (fd, read_fd, write_fd):
        if candidate >= 0:
            os.close(candidate)

if bytes_moved <= 0 or payload != b"hello via splice":
    raise SystemExit(1)
""".lstrip(),
        encoding="utf-8",
    )

    report = _run_under_tracer(
        tracer,
        [sys.executable, str(workload_src), str(src)],
        cwd=tmp_path,
    )

    read = _read(report)
    assert str(src.resolve()) in read, f"splice source not marked read; read={sorted(read)}"
