#!/usr/bin/env python3
"""End-to-end sandbox for the three roar tracers.

Builds a tiny Python workload that reads `input.txt` and writes `output.txt`,
then runs it under each tracer backend (ptrace / preload / ebpf) and checks
that both files appear in the resulting msgpack report.

Why a Python workload instead of `sh -c`: there is a known tracer race where
short-lived shell-spawned writes (e.g. `bash -c "echo > x"`) can complete
before the tracer attaches/observes the syscall. A single in-process Python
read+write keeps everything on one well-defined process.

Usage:
    python3 scripts/test_tracers_sandbox.py
    python3 scripts/test_tracers_sandbox.py --backend ptrace
    python3 scripts/test_tracers_sandbox.py --keep-tmp     # leave artifacts behind
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = REPO_ROOT / "rust" / "target" / "release"
DEBUG_DIR = REPO_ROOT / "rust" / "target" / "debug"
PACKAGED_BIN_DIR = REPO_ROOT / "roar" / "bin"

WORKLOAD_SOURCE = """\
import sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, 'rb') as f:
    data = f.read()
with open(dst, 'wb') as f:
    f.write(b'roar-sandbox:' + data)
"""

ALL_BACKENDS = ("ptrace", "preload", "ebpf")


@dataclass
class BackendResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


def _find_binary(name: str) -> Path | None:
    for d in (RELEASE_DIR, DEBUG_DIR, PACKAGED_BIN_DIR):
        p = d / name
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


def _import_msgpack():
    try:
        import msgpack  # type: ignore[import-not-found]
    except ImportError:
        sys.stderr.write("msgpack not installed. Install it with: pip install msgpack\n")
        sys.exit(2)
    return msgpack


def _parse_report(path: Path) -> dict:
    msgpack = _import_msgpack()
    return msgpack.unpackb(path.read_bytes(), raw=False)


def _files_were_seen(report: dict, *, expect_read: Path, expect_written: Path) -> tuple[bool, str]:
    read_files = set(report.get("read_files") or [])
    written_files = set(report.get("written_files") or [])
    files = report.get("files") or []
    for entry in files:
        if entry.get("read"):
            read_files.add(entry["path"])
        if entry.get("written"):
            written_files.add(entry["path"])

    src = str(expect_read.resolve())
    dst = str(expect_written.resolve())
    missing = []
    if src not in read_files:
        missing.append(f"read:{src}")
    if dst not in written_files:
        missing.append(f"written:{dst}")
    if missing:
        return False, "missing entries: " + ", ".join(missing)
    return True, f"saw read+write of {expect_read.name}/{expect_written.name}"


def _build_workload(workdir: Path) -> tuple[Path, Path, Path]:
    src = workdir / "input.txt"
    dst = workdir / "output.txt"
    workload = workdir / "workload.py"
    src.write_bytes(b"hello-from-aarch64-sandbox\n")
    workload.write_text(WORKLOAD_SOURCE)
    return workload, src, dst


def _run_tracer(
    tracer: Path,
    *,
    output: Path,
    workload: Path,
    src: Path,
    dst: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(tracer), str(output), sys.executable, str(workload), str(src), str(dst)],
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def run_ptrace(workdir: Path) -> BackendResult:
    binary = _find_binary("roar-tracer")
    if binary is None:
        return BackendResult("ptrace", "skip", "roar-tracer not built")

    workload, src, dst = _build_workload(workdir)
    output = workdir / "ptrace.msgpack"
    try:
        proc = _run_tracer(binary, output=output, workload=workload, src=src, dst=dst)
    except subprocess.TimeoutExpired:
        return BackendResult("ptrace", "fail", "tracer timed out")

    if proc.returncode != 0 or not output.exists():
        return BackendResult(
            "ptrace",
            "fail",
            f"exit={proc.returncode} stderr={proc.stderr.decode(errors='replace')[:200]}",
        )

    ok, detail = _files_were_seen(_parse_report(output), expect_read=src, expect_written=dst)
    return BackendResult("ptrace", "pass" if ok else "fail", detail)


def run_preload(workdir: Path) -> BackendResult:
    binary = _find_binary("roar-tracer-preload")
    if binary is None:
        return BackendResult("preload", "skip", "roar-tracer-preload not built")

    workload, src, dst = _build_workload(workdir)
    output = workdir / "preload.msgpack"
    try:
        proc = _run_tracer(binary, output=output, workload=workload, src=src, dst=dst)
    except subprocess.TimeoutExpired:
        return BackendResult("preload", "fail", "tracer timed out")

    if proc.returncode != 0 or not output.exists():
        return BackendResult(
            "preload",
            "fail",
            f"exit={proc.returncode} stderr={proc.stderr.decode(errors='replace')[:200]}",
        )

    ok, detail = _files_were_seen(_parse_report(output), expect_read=src, expect_written=dst)
    return BackendResult("preload", "pass" if ok else "fail", detail)


def run_ebpf(workdir: Path) -> BackendResult:
    if platform.system() != "Linux":
        return BackendResult("ebpf", "skip", "ebpf is Linux-only")

    binary = _find_binary("roar-tracer-ebpf")
    roard = _find_binary("roard")
    if binary is None:
        return BackendResult("ebpf", "skip", "roar-tracer-ebpf not built")
    if roard is None:
        return BackendResult("ebpf", "skip", "roard not built")

    workload, src, dst = _build_workload(workdir)
    output = workdir / "ebpf.msgpack"

    runtime_dir = workdir / "xdg-runtime"
    runtime_dir.mkdir(exist_ok=True)
    runtime_dir.chmod(0o700)

    extra_env = {
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "PATH": f"{roard.parent}:{os.environ.get('PATH', '')}",
    }

    try:
        proc = _run_tracer(
            binary,
            output=output,
            workload=workload,
            src=src,
            dst=dst,
            extra_env=extra_env,
        )
    except subprocess.TimeoutExpired:
        return BackendResult("ebpf", "fail", "tracer timed out")

    stderr = proc.stderr.decode(errors="replace")
    if "Operation not permitted" in stderr or proc.returncode != 0 or not output.exists():
        # eBPF needs CAP_BPF / CAP_PERFMON; flag rather than fail in unprivileged sandboxes.
        if "Operation not permitted" in stderr or "permission" in stderr.lower():
            return BackendResult(
                "ebpf",
                "skip",
                "no CAP_BPF (run as root or with required capabilities)",
            )
        return BackendResult("ebpf", "fail", f"exit={proc.returncode} stderr={stderr[:200]}")

    ok, detail = _files_were_seen(_parse_report(output), expect_read=src, expect_written=dst)
    return BackendResult("ebpf", "pass" if ok else "fail", detail)


RUNNERS = {"ptrace": run_ptrace, "preload": run_preload, "ebpf": run_ebpf}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend",
        choices=("all", *ALL_BACKENDS),
        default="all",
        help="which backend(s) to test (default: all)",
    )
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="don't remove the temporary working directory",
    )
    args = parser.parse_args()

    backends = ALL_BACKENDS if args.backend == "all" else (args.backend,)

    workdir = Path(tempfile.mkdtemp(prefix="roar-tracer-sandbox-"))
    print(f"sandbox: {workdir}  arch={platform.machine()}")

    results: list[BackendResult] = []
    try:
        for name in backends:
            backend_dir = workdir / name
            backend_dir.mkdir()
            results.append(RUNNERS[name](backend_dir))
    finally:
        if not args.keep_tmp:
            shutil.rmtree(workdir, ignore_errors=True)

    width = max(len(r.name) for r in results)
    for r in results:
        marker = {"pass": "OK  ", "fail": "FAIL", "skip": "SKIP"}[r.status]
        print(f"  {marker}  {r.name:<{width}}  {r.detail}")

    failed = [r for r in results if r.status == "fail"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
