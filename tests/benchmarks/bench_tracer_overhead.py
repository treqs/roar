#!/usr/bin/env python3
"""
Benchmark: decompose tracer overhead into startup vs. marginal cost.

Measures:
  1. Fixed startup/teardown cost (trace true)
  2. Marginal per-file cost (1 write + 1 read per file at increasing file counts)
  3. Marginal per-read/write cost (fixed files, increasing read/write repetitions)

Reports absolute milliseconds for each component per tracer (ptrace, preload, eBPF).
"""

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Configuration
TRUE_BIN = shutil.which("true") or "/usr/bin/true"
PYTHON_BIN = ".venv/bin/python"
TRACER_BIN = "roar/bin/roar-tracer"
PRELOAD_TRACER_BIN = os.environ.get(
    "PRELOAD_TRACER_BIN",
    os.path.join(os.path.dirname(__file__), "../../rust/target/release/roar-tracer-preload"),
)
EBPF_TRACER_BIN = os.environ.get(
    "EBPF_TRACER_BIN",
    os.path.join(os.path.dirname(__file__), "../../rust/target/release/roar-tracer-ebpf"),
)
ROARD_BIN = os.environ.get(
    "ROARD_BIN",
    os.path.join(os.path.dirname(__file__), "../../rust/target/release/roard"),
)
NUM_ITERATIONS = 10
NUM_WARMUP = 2
EBPF_SLEEP = 1.5  # seconds between eBPF runs for BPF cleanup

# Scale points for per-file series: number of files to open/write + open/read.
SYSCALL_SCALES = [0, 200, 500, 1000, 2000, 5000]
# Scale points for per-read/write series: repeated writes+reads per file.
RW_PER_FILE_SCALES = [1, 2, 4, 8, 16, 32, 64]
RW_SERIES_FILE_COUNT = 200

EXPECTED_CAP_NAMES = {
    "cap_bpf",
    "cap_dac_read_search",
    "cap_perfmon",
    "cap_sys_ptrace",
    "cap_sys_resource",
}


def _get_current_caps(path):
    """Get the current capabilities set on a binary."""
    try:
        result = subprocess.run(["getcap", path], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                caps_str = parts[-1].split("=")[0]
                return {c.strip() for c in caps_str.split(",")}
    except FileNotFoundError:
        pass
    return set()


def _has_caps(binary_path):
    """Check whether the eBPF binary has required capabilities."""
    caps = _get_current_caps(binary_path)
    return EXPECTED_CAP_NAMES.issubset(caps)


def _has_passwordless_sudo():
    """Check if the current user can run sudo without a password."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _ebpf_works_direct(binary_path):
    """Quick test: can the eBPF binary run directly (without sudo)?"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            r = subprocess.run(
                [binary_path, f.name, TRUE_BIN],
                capture_output=True,
                timeout=10,
                text=True,
            )
            return r.returncode == 0
    except Exception:
        return False


def _ebpf_works_sudo(binary_path):
    """Quick test: can the eBPF binary run via sudo?"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            r = subprocess.run(
                ["sudo", "-n", binary_path, f.name, TRUE_BIN],
                capture_output=True,
                timeout=10,
                text=True,
            )
            return r.returncode == 0
    except Exception:
        return False


def _find_preload_library(binary_path):
    """Find a preload shared library next to the launcher."""
    binary_dir = Path(binary_path).parent
    direct_candidates = (
        binary_dir / "libroar_tracer_preload.so",
        binary_dir / "libroar-tracer-preload.so",
        binary_dir / "libroar_tracer_preload.dylib",
        binary_dir / "libroar-tracer-preload.dylib",
    )
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate)

    wildcard_candidates = []
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar_tracer_preload*.so")))
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar-tracer-preload*.so")))
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar_tracer_preload*.dylib")))
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar-tracer-preload*.dylib")))
    for candidate in wildcard_candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _preload_works(binary_path, preload_lib=None):
    """Quick test: can the preload binary run directly. Returns (ok, error_msg)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            env = os.environ.copy()
            if preload_lib:
                env["ROAR_PRELOAD_LIB"] = preload_lib
            r = subprocess.run(
                [binary_path, f.name, TRUE_BIN],
                capture_output=True,
                timeout=10,
                text=True,
                env=env,
            )
            if r.returncode == 0:
                return True, ""
            msg = (
                r.stderr.strip().split("\n")[0][:200]
                if r.stderr.strip()
                else f"exit code {r.returncode}"
            )
            return False, msg
    except Exception as e:
        return False, str(e)


def start_roard(sudo_prefix):
    """Start roard daemon and wait for it to be ready. Returns the process."""
    if not os.path.exists(ROARD_BIN):
        print(f"  roard not found at {ROARD_BIN}, skipping daemon benchmarks", file=sys.stderr)
        return None
    sock = _roard_socket_path()
    # Clean up stale socket
    for ext in ("", ".pid"):
        p = sock + ext if ext else sock
        if os.path.exists(p):
            os.unlink(p)
    cmd = [*sudo_prefix, ROARD_BIN, "--idle-timeout", "300", "--socket", sock]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for socket to appear
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if os.path.exists(sock):
            return proc
        time.sleep(0.05)
    proc.kill()
    print("  roard failed to start, skipping daemon benchmarks", file=sys.stderr)
    return None


def stop_roard(proc):
    """Stop the roard daemon."""
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    sock = _roard_socket_path()
    for p in (sock, sock + ".pid"):
        if os.path.exists(p):
            os.unlink(p)


def _roard_socket_path():
    """Mirror the socket path logic from ipc.rs."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "roard.sock")
    return f"/tmp/roard-{os.getuid()}.sock"


def make_scaling_script(n):
    """Workload: create N files (write), then read them all back."""
    if n == 0:
        return "pass\n"
    return (
        "import os, tempfile, shutil\n"
        "workdir = tempfile.mkdtemp(prefix='bench_scale_')\n"
        "try:\n"
        f"    for i in range({n}):\n"
        "        with open(os.path.join(workdir, f'f_{i}'), 'w') as f:\n"
        "            f.write('x' * 256)\n"
        f"    for i in range({n}):\n"
        "        with open(os.path.join(workdir, f'f_{i}'), 'r') as f:\n"
        "            f.read()\n"
        "finally:\n"
        "    shutil.rmtree(workdir)\n"
    )


def make_rw_scaling_script(n_files, rw_per_file):
    """Workload: fixed files, repeated writes then reads per file."""
    if n_files == 0 or rw_per_file == 0:
        return "pass\n"
    return (
        "import os, tempfile, shutil\n"
        "workdir = tempfile.mkdtemp(prefix='bench_rw_scale_')\n"
        "payload = b'x' * 256\n"
        f"paths = [os.path.join(workdir, 'f_' + str(i)) for i in range({n_files})]\n"
        "try:\n"
        "    for path in paths:\n"
        "        with open(path, 'wb') as f:\n"
        "            f.write(payload)\n"
        f"    for _ in range({rw_per_file}):\n"
        "        for path in paths:\n"
        "            with open(path, 'r+b') as f:\n"
        "                f.seek(0)\n"
        "                f.write(payload)\n"
        "        for path in paths:\n"
        "            with open(path, 'rb') as f:\n"
        "                f.read(len(payload))\n"
        "finally:\n"
        "    shutil.rmtree(workdir)\n"
    )


def run_timed(cmd, timeout=120, env=None):
    """Run command, return (elapsed_seconds, success, stderr)."""
    start = time.perf_counter()
    merged_env = None if env is None else {**os.environ, **env}
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True, env=merged_env)
        return time.perf_counter() - start, r.returncode == 0, r.stderr
    except subprocess.TimeoutExpired:
        return time.perf_counter() - start, False, "timed out"
    except Exception as e:
        return 0.0, False, str(e)


def measure(label, cmd, n_iter, n_warmup, sleep_between=0.0, env=None):
    """Warmup then measure n_iter runs. Returns list of elapsed times (s)."""
    for _ in range(n_warmup):
        run_timed(cmd, env=env)
        if sleep_between:
            time.sleep(sleep_between)
    times = []
    fail_msg_shown = False
    for _i in range(n_iter):
        t, ok, stderr = run_timed(cmd, env=env)
        if ok:
            times.append(t)
        else:
            if not fail_msg_shown:
                msg = stderr.strip().split("\n")[0][:120] if stderr.strip() else "unknown"
                print(f"  FAIL: {label}: {msg}", file=sys.stderr, flush=True)
                fail_msg_shown = True
        if sleep_between:
            time.sleep(sleep_between)
    return times


def ms(seconds):
    return seconds * 1000


def mean_std(times):
    if not times:
        return 0.0, 0.0
    return statistics.mean(times), (statistics.stdev(times) if len(times) > 1 else 0.0)


def linear_regression(xs, ys):
    """OLS: y = intercept + slope * x. Returns (intercept, slope)."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        return sy / n, 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return intercept, slope


def fmt_row(name, total, overhead, stddev):
    """Format one row of the Part 1 table."""
    return f"  {name:>14}  {total:>10}  {overhead:>10}  {stddev:>8}"


def main():
    print("=" * 72)
    print("Tracer Overhead: Startup vs. Marginal Cost")
    print("=" * 72)
    print(f"  Iterations: {NUM_ITERATIONS}  Warmup: {NUM_WARMUP}")
    print(f"  ptrace: {TRACER_BIN}")
    print(f"  preload:{PRELOAD_TRACER_BIN}")
    print(f"  ebpf:   {EBPF_TRACER_BIN}")
    print(f"  roard:  {ROARD_BIN}")
    print(f"  python: {PYTHON_BIN}")
    print(f"  per-file scales: {SYSCALL_SCALES}")
    print(f"  per-rw scales:   {RW_PER_FILE_SCALES}  (fixed files={RW_SERIES_FILE_COUNT})")

    if not os.path.exists(PYTHON_BIN):
        print(f"ERROR: python not found: {PYTHON_BIN}", file=sys.stderr)
        sys.exit(1)

    # ── Determine how to run the eBPF binary ─────────────────────────────
    ebpf_sudo = []
    ebpf_available = False

    if not os.path.exists(EBPF_TRACER_BIN):
        print("  ebpf:   NOT BUILT (binary not found)")
    elif _has_caps(EBPF_TRACER_BIN):
        print("  ebpf:   capabilities set")
        ebpf_available = True
    elif os.getuid() == 0:
        print("  ebpf:   running as root")
        ebpf_available = True
    elif _ebpf_works_direct(EBPF_TRACER_BIN):
        print("  ebpf:   works without capabilities (perhaps sysctl?)")
        ebpf_available = True
    elif _has_passwordless_sudo() and _ebpf_works_sudo(EBPF_TRACER_BIN):
        print("  ebpf:   using sudo (passwordless)")
        ebpf_sudo = ["sudo", "-n"]
        ebpf_available = True
    else:
        print("  ebpf:   UNAVAILABLE (no caps, no sudo)")
        print("          To fix: run as root, set capabilities, or enable passwordless sudo")

    print("=" * 72, flush=True)

    # ── Build tracer configs ─────────────────────────────────────────────
    active_tracers = {}

    if os.path.exists(TRACER_BIN):
        active_tracers["ptrace"] = {"bin": TRACER_BIN, "sleep": 0.0, "sudo": []}
    else:
        print("  ptrace: NOT BUILT (binary not found)", flush=True)

    preload_lib = (
        _find_preload_library(PRELOAD_TRACER_BIN) if os.path.exists(PRELOAD_TRACER_BIN) else None
    )
    preload_env = {"ROAR_PRELOAD_LIB": preload_lib} if preload_lib else None
    if not os.path.exists(PRELOAD_TRACER_BIN):
        print("  preload: NOT BUILT (binary not found)", flush=True)
    elif preload_lib is None:
        print("  preload: NOT BUILT (shared library not found)", flush=True)
    else:
        print(f"  preload: enabled (lib={preload_lib})", flush=True)
        active_tracers["preload"] = {
            "bin": PRELOAD_TRACER_BIN,
            "sleep": 0.0,
            "sudo": [],
            "env": preload_env,
        }

    if ebpf_available:
        active_tracers["ebpf"] = {
            "bin": EBPF_TRACER_BIN,
            "sleep": EBPF_SLEEP,
            "sudo": ebpf_sudo,
        }

        # eBPF daemon — start roard first
        roard_proc = start_roard(ebpf_sudo)
        if roard_proc is not None:
            # The daemon client inside roar-tracer-ebpf computes the socket
            # path from its own UID. When running via sudo, that's UID 0.
            # Pass the socket path via env so the client can find the right one.
            daemon_env_sock = _roard_socket_path()
            active_tracers["ebpf-daemon"] = {
                "bin": EBPF_TRACER_BIN,
                "sleep": 0.0,
                "sudo": ebpf_sudo,
                "daemon": True,
                "daemon_sock": daemon_env_sock,
            }
        else:
            print("  Skipping ebpf-daemon (roard unavailable)", flush=True)
            roard_proc = None
    else:
        print("  Skipping ebpf and ebpf-daemon benchmarks", flush=True)
        roard_proc = None

    try:
        _run_benchmarks(active_tracers)
    finally:
        stop_roard(roard_proc)


def _run_benchmarks(active_tracers):
    if not active_tracers:
        print("\nNo tracers available to benchmark.", file=sys.stderr)
        sys.exit(1)

    # ==================================================================
    # Part 1: Fixed startup/teardown cost  (trace true)
    # ==================================================================
    print("\n--- Part 1: Startup / teardown (tracing true) ---\n")

    bl = measure("baseline true", [TRUE_BIN], NUM_ITERATIONS, NUM_WARMUP)
    bl_mean, bl_std = mean_std(bl)

    startup = {}  # name -> overhead_ms
    print(fmt_row("", "Total", "Overhead", "Stddev") + "   (ms)")
    print(fmt_row("", "-----", "--------", "------"))
    print(fmt_row("baseline", f"{ms(bl_mean):.1f}", "--", f"{ms(bl_std):.1f}"))

    for name, cfg in active_tracers.items():
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            cmd = [*cfg["sudo"], cfg["bin"], f.name, TRUE_BIN]
            times = measure(
                f"{name} true",
                cmd,
                NUM_ITERATIONS,
                NUM_WARMUP,
                sleep_between=cfg["sleep"],
                env=cfg.get("env"),
            )
        tm, ts = mean_std(times)
        overhead = tm - bl_mean
        startup[name] = ms(overhead)
        print(fmt_row(name, f"{ms(tm):.1f}", f"{ms(overhead):+.1f}", f"{ms(ts):.1f}"))

    # ==================================================================
    # Part 2: Marginal per-file cost — scaling workload by file count
    # ==================================================================
    print("\n--- Part 2: Marginal per-file cost (N = files written + read once) ---\n")

    with tempfile.TemporaryDirectory(prefix="bench_scale_") as tmpdir:
        workdir = Path(tmpdir)

        # Store (N_files, overhead_ms) per tracer
        per_file_points = {name: [] for name in active_tracers}
        table_rows = []

        for n in SYSCALL_SCALES:
            script = workdir / f"scale_{n}.py"
            script.write_text(make_scaling_script(n))

            row = {"N": n}

            # Baseline
            bl = measure(f"baseline N={n}", [PYTHON_BIN, str(script)], NUM_ITERATIONS, NUM_WARMUP)
            bl_mean, bl_std = mean_std(bl)
            row["baseline"] = ms(bl_mean)
            row["baseline_std"] = ms(bl_std)

            for name, cfg in active_tracers.items():
                with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
                    cmd = [*cfg["sudo"], cfg["bin"], f.name, PYTHON_BIN, str(script)]
                    times = measure(
                        f"{name} N={n}",
                        cmd,
                        NUM_ITERATIONS,
                        NUM_WARMUP,
                        sleep_between=cfg["sleep"],
                        env=cfg.get("env"),
                    )
                tm, ts = mean_std(times)
                overhead = ms(tm - bl_mean)
                per_file_points[name].append((n, overhead))
                row[f"{name}_overhead"] = overhead
                row[f"{name}_std"] = ms(ts)

            table_rows.append(row)
            parts = [f"  N={n:>5}  base={row['baseline']:>7.1f}"]
            for name in active_tracers:
                parts.append(
                    f"{name}=+{row[f'{name}_overhead']:>6.1f}\u00b1{row[f'{name}_std']:.1f}"
                )
            print("  ".join(parts) + "  (ms)", flush=True)

    # ==================================================================
    # Regression A: per-file series
    # ==================================================================
    print("\n--- Regression A: overhead(N_files) = startup_ms + per_file_ms * N_files ---\n")

    per_file_regressions = {}
    print(f"  {'Tracer':>14}  {'Startup (ms)':>13}  {'Per-file (ms)':>14}  {'Per-file (us)':>14}")
    print(f"  {'------':>14}  {'------------':>13}  {'-------------':>14}  {'-------------':>14}")
    for name, points in per_file_points.items():
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        intercept, slope = linear_regression(xs, ys)
        per_file_regressions[name] = (intercept, slope)
        print(f"  {name:>14}  {intercept:>13.1f}  {slope:>14.4f}  {slope * 1000:>14.1f}")

    # ==================================================================
    # Part 3: Marginal per-read/write cost — fixed files, varying repeats
    # ==================================================================
    print(
        f"\n--- Part 3: Marginal per-read/write cost "
        f"(fixed files={RW_SERIES_FILE_COUNT}, varying reads+writes per file) ---\n"
    )

    with tempfile.TemporaryDirectory(prefix="bench_rw_scale_") as tmpdir:
        workdir = Path(tmpdir)

        # Store (total_rw_ops, overhead_ms) per tracer
        per_rw_points = {name: [] for name in active_tracers}

        for rw_per_file in RW_PER_FILE_SCALES:
            script = workdir / f"rw_scale_{RW_SERIES_FILE_COUNT}_{rw_per_file}.py"
            script.write_text(make_rw_scaling_script(RW_SERIES_FILE_COUNT, rw_per_file))
            total_rw_ops = 2 * RW_SERIES_FILE_COUNT * rw_per_file

            # Baseline
            bl = measure(
                f"baseline rw/file={rw_per_file}",
                [PYTHON_BIN, str(script)],
                NUM_ITERATIONS,
                NUM_WARMUP,
            )
            bl_mean, bl_std = mean_std(bl)

            parts = [
                f"  rw/file={rw_per_file:>3}",
                f"ops={total_rw_ops:>6}",
                f"base={ms(bl_mean):>7.1f}",
            ]

            for name, cfg in active_tracers.items():
                with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
                    cmd = [*cfg["sudo"], cfg["bin"], f.name, PYTHON_BIN, str(script)]
                    times = measure(
                        f"{name} rw/file={rw_per_file}",
                        cmd,
                        NUM_ITERATIONS,
                        NUM_WARMUP,
                        sleep_between=cfg["sleep"],
                        env=cfg.get("env"),
                    )
                tm, ts = mean_std(times)
                overhead = ms(tm - bl_mean)
                per_rw_points[name].append((total_rw_ops, overhead))
                overhead_us_per_op = (overhead * 1000 / total_rw_ops) if total_rw_ops else 0.0
                parts.append(
                    f"{name}=+{overhead:>6.1f}±{ms(ts):.1f} ({overhead_us_per_op:.2f} us/op)"
                )

            print("  ".join(parts) + "  (ms)", flush=True)

    # ==================================================================
    # Regression B: per-read/write series
    # ==================================================================
    print("\n--- Regression B: overhead(ops) = intercept_ms + per_rw_ms * total_rw_ops ---\n")

    per_rw_regressions = {}
    print(f"  {'Tracer':>14}  {'Intercept (ms)':>14}  {'Per-rw (ms)':>12}  {'Per-rw (us)':>12}")
    print(f"  {'------':>14}  {'--------------':>14}  {'-----------':>12}  {'-----------':>12}")
    for name, points in per_rw_points.items():
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        intercept, slope = linear_regression(xs, ys)
        per_rw_regressions[name] = (intercept, slope)
        print(f"  {name:>14}  {intercept:>14.1f}  {slope:>12.6f}  {slope * 1000:>12.3f}")

    # ==================================================================
    # Summary
    # ==================================================================
    print(f"\n{'=' * 72}")
    print("SUMMARY")
    print(f"{'=' * 72}\n")

    for name in active_tracers:
        intercept, slope = per_file_regressions[name]
        rw_intercept, rw_slope = per_rw_regressions[name]
        true_startup = startup[name]
        print(f"  {name}:")
        print(f"    Startup (true):          {true_startup:>8.1f} ms")
        print(f"    Startup (file regression):    {intercept:>8.1f} ms")
        print(f"    Marginal cost per file:       {slope:>8.4f} ms  ({slope * 1000:.1f} us)")
        print(f"    Per-rw intercept:             {rw_intercept:>8.1f} ms")
        print(f"    Marginal cost per read/write: {rw_slope:>8.6f} ms  ({rw_slope * 1000:.3f} us)")
        print("    Projected overhead by files:")
        for nf in [100, 1000, 10000, 100000]:
            total = intercept + slope * nf
            print(f"      N={nf:>6} files:            {total:>8.1f} ms")
        print("    Projected overhead by read/write ops:")
        for nops in [1000, 10000, 100000, 1000000]:
            total = rw_intercept + rw_slope * nops
            print(f"      ops={nops:>7}:              {total:>8.1f} ms")
        print()


if __name__ == "__main__":
    main()
