"""Regression coverage for eBPF tracking of splice(2) reads."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
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
    pytest.mark.skipif(not hasattr(os, "splice"), reason="Python lacks os.splice"),
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


def _read_paths(report_path: Path) -> set[str]:
    data = msgpack.unpackb(report_path.read_bytes(), raw=False)
    paths = set(data.get("read_files") or [])
    for entry in data.get("files") or []:
        if entry.get("read"):
            paths.add(entry["path"])
    return paths


def test_splice_file_to_pipe_records_source_as_read(tmp_path: Path) -> None:
    tracer, roard = _ensure_binaries()
    src = tmp_path / "input.txt"
    report = tmp_path / "report.msgpack"
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

    env = os.environ.copy()
    env["PATH"] = f"{roard.parent}:{env.get('PATH', '')}"
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [str(tracer), str(report), sys.executable, str(workload_src), str(src)],
        cwd=str(tmp_path),
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0, (
        f"tracer exited {proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )

    read = _read_paths(report)
    assert str(src.resolve()) in read, f"splice source not marked read; read={sorted(read)}"
