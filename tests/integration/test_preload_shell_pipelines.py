"""Regression coverage for the preload-tracer shell-pipeline gap (P0-5).

The preload backend used to miss every redirect target written via shell
builtins or printf-family symbols (`bash -c 'echo > x'`, `awk > x`, plus
chained shell scripts) — the C-side `open`/`openat` interposers in
`interpose.c` were silently linker-stripped from the .so because rustc's
cdylib version-script hides anything not declared as a Rust
`#[no_mangle] pub extern`. The exported `read`/`write` hooks didn't
help because bash's redirect output goes through libc-internal `__write`
that bypasses the PLT.

The fix moves the C interposer logic into Rust shims that re-export
each `open`/`openat`/`open64`/`openat64`/`creat` with `#[no_mangle]`
(forwarding to internal `roar_libc_*_impl` C helpers for the variadic
+ dlsym(RTLD_NEXT) parts) and absolutizes paths in the tracee before
emitting events.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

import tests.conftest as test_conftest

try:
    import msgpack  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    msgpack = None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="preload tracer requires Linux"),
    pytest.mark.skipif(msgpack is None, reason="msgpack not installed"),
]


def _ensure_preload() -> Path:
    return test_conftest._ensure_repo_local_preload_tracer()


def _run(tracer: Path, command: list[str], cwd: Path) -> dict:
    report = cwd / "report.msgpack"
    proc = subprocess.run(
        [str(tracer), str(report), *command],
        cwd=str(cwd),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"tracer exit={proc.returncode}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    return msgpack.unpackb(report.read_bytes(), raw=False)


def _written(report: dict) -> set[str]:
    paths = set(report.get("written_files") or [])
    for entry in report.get("files") or []:
        if entry.get("written"):
            paths.add(entry["path"])
    return paths


def _read(report: dict) -> set[str]:
    paths = set(report.get("read_files") or [])
    for entry in report.get("files") or []:
        if entry.get("read"):
            paths.add(entry["path"])
    return paths


def test_bash_single_redirect(tmp_path: Path) -> None:
    tracer = _ensure_preload()
    target = tmp_path / "out.txt"
    report = _run(tracer, ["bash", "-c", f"echo hi > {target.name}"], cwd=tmp_path)
    assert str(target.resolve()) in _written(report), (
        f"bash redirect missed: written={sorted(_written(report))}"
    )


def test_pipe_to_redirect(tmp_path: Path) -> None:
    tracer = _ensure_preload()
    target = tmp_path / "out.txt"
    report = _run(
        tracer,
        ["bash", "-c", f"echo a b c | tr ' ' '\\n' | sort | head -n 2 > {target.name}"],
        cwd=tmp_path,
    )
    assert str(target.resolve()) in _written(report)


def test_awk_redirect_then_cat(tmp_path: Path) -> None:
    """The exact P0-5 stage-3 reproducer: an awk write followed by a
    cat read of the same file must be captured as both Read and Write,
    not Read-only."""
    tracer = _ensure_preload()
    target = tmp_path / "out.txt"
    cmd = f"echo abc | awk '{{print toupper($0)}}' > {target.name}; cat {target.name} > /dev/null"
    report = _run(tracer, ["bash", "-c", cmd], cwd=tmp_path)
    target_abs = str(target.resolve())
    assert target_abs in _written(report), (
        f"awk redirect missed; this is the P0-5 stage-3 case. written={sorted(_written(report))}"
    )
    assert target_abs in _read(report), (
        f"cat read missed; report inconsistent. read={sorted(_read(report))}"
    )


def test_three_stage_pipeline(tmp_path: Path) -> None:
    """Mimics the P0-5 gen_corpus → summarize → score chained pipeline:
    each stage's output must be both Written (by the producing stage)
    and Read (by the consuming stage)."""
    tracer = _ensure_preload()
    cmd = (
        "echo 'one\ntwo\nthree' > corpus.txt && "
        "wc -l corpus.txt | awk '{print $1}' > summary.txt && "
        "awk '{print \"score=\" $1*2}' summary.txt > score.txt"
    )
    report = _run(tracer, ["bash", "-c", cmd], cwd=tmp_path)

    written = _written(report)
    read = _read(report)
    for name in ("corpus.txt", "summary.txt", "score.txt"):
        full = str((tmp_path / name).resolve())
        assert full in written, f"{name} not in written; written={sorted(written)}"

    # Intermediate stage outputs must also be readable by the next stage.
    for name in ("corpus.txt", "summary.txt"):
        full = str((tmp_path / name).resolve())
        assert full in read, f"{name} not in read; read={sorted(read)}"
