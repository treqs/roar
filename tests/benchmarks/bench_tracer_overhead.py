#!/usr/bin/env python3
"""
Benchmark: decompose tracer overhead into startup vs. marginal cost.

Measures:
  1. Fixed startup/teardown cost (trace /bin/true)
  2. Marginal per-syscall cost (trace open/read/write/close at increasing scales)

Reports absolute milliseconds for each component per tracer (ptrace, preload, eBPF).
"""

import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Configuration
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

# Scale points: number of files to open/write + open/read per iteration
SYSCALL_SCALES = [0, 200, 500, 1000, 2000, 5000]

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
                [binary_path, f.name, "/bin/true"],
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
                ["sudo", "-n", binary_path, f.name, "/bin/true"],
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
    )
    for candidate in direct_candidates:
        if candidate.exists():
            return str(candidate)

    wildcard_candidates = []
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar_tracer_preload*.so")))
    wildcard_candidates.extend(sorted(binary_dir.glob("libroar-tracer-preload*.so")))
    for candidate in wildcard_candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _preload_works(binary_path, preload_lib=None):
    """Quick test: can the preload binary run directly."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            env = os.environ.copy()
            if preload_lib:
                env["ROAR_PRELOAD_LIB"] = preload_lib
            r = subprocess.run(
                [binary_path, f.name, "/bin/true"],
                capture_output=True,
                timeout=10,
                text=True,
                env=env,
            )
            return r.returncode == 0
    except Exception:
        return False


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

    for path, label in [
        (TRACER_BIN, "ptrace"),
        (PRELOAD_TRACER_BIN, "preload"),
        (EBPF_TRACER_BIN, "eBPF"),
        (PYTHON_BIN, "python"),
    ]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}", file=sys.stderr)
            sys.exit(1)

    # ── Determine how to run the eBPF binary ─────────────────────────────
    ebpf_sudo = []
    ebpf_available = False

    if _has_caps(EBPF_TRACER_BIN):
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
    active_tracers["ptrace"] = {"bin": TRACER_BIN, "sleep": 0.0, "sudo": []}
    preload_lib = _find_preload_library(PRELOAD_TRACER_BIN)
    preload_env = {"ROAR_PRELOAD_LIB": preload_lib} if preload_lib else None
    if _preload_works(PRELOAD_TRACER_BIN, preload_lib):
        preload_note = f" (lib={preload_lib})" if preload_lib else ""
        print(f"  preload: enabled{preload_note}", flush=True)
        active_tracers["preload"] = {
            "bin": PRELOAD_TRACER_BIN,
            "sleep": 0.0,
            "sudo": [],
            "env": preload_env,
        }
    else:
        print("  preload: UNAVAILABLE (launcher/library not runnable)", flush=True)

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
    # ==================================================================
    # Part 1: Fixed startup/teardown cost  (trace /bin/true)
    # ==================================================================
    print("\n--- Part 1: Startup / teardown (tracing /bin/true) ---\n")

    bl = measure("baseline /bin/true", ["/bin/true"], NUM_ITERATIONS, NUM_WARMUP)
    bl_mean, bl_std = mean_std(bl)

    startup = {}  # name -> overhead_ms
    print(fmt_row("", "Total", "Overhead", "Stddev") + "   (ms)")
    print(fmt_row("", "-----", "--------", "------"))
    print(fmt_row("baseline", f"{ms(bl_mean):.1f}", "--", f"{ms(bl_std):.1f}"))

    for name, cfg in active_tracers.items():
        with tempfile.NamedTemporaryFile(suffix=".msgpack", delete=True) as f:
            cmd = [*cfg["sudo"], cfg["bin"], f.name, "/bin/true"]
            times = measure(
                f"{name} /bin/true",
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
    # Part 2: Marginal cost — scaling workload (python, N files)
    # ==================================================================
    print("\n--- Part 2: Marginal cost (N = files written + read) ---\n")

    with tempfile.TemporaryDirectory(prefix="bench_scale_") as tmpdir:
        workdir = Path(tmpdir)

        # Store (N, overhead_ms) per tracer
        scale_points = {name: [] for name in active_tracers}
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
                scale_points[name].append((n, overhead))
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
    # Regression
    # ==================================================================
    print("\n--- Regression: overhead(N) = startup_ms + per_file_ms * N ---\n")

    regressions = {}
    print(f"  {'Tracer':>14}  {'Startup (ms)':>13}  {'Per-file (ms)':>14}  {'Per-file (us)':>14}")
    print(f"  {'------':>14}  {'------------':>13}  {'-------------':>14}  {'-------------':>14}")
    for name, points in scale_points.items():
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        intercept, slope = linear_regression(xs, ys)
        regressions[name] = (intercept, slope)
        print(f"  {name:>14}  {intercept:>13.1f}  {slope:>14.4f}  {slope * 1000:>14.1f}")

    # ==================================================================
    # Summary
    # ==================================================================
    print(f"\n{'=' * 72}")
    print("SUMMARY")
    print(f"{'=' * 72}\n")

    for name in active_tracers:
        intercept, slope = regressions[name]
        true_startup = startup[name]
        print(f"  {name}:")
        print(f"    Startup (/bin/true):          {true_startup:>8.1f} ms")
        print(f"    Startup (regression):         {intercept:>8.1f} ms")
        print(f"    Marginal cost per file:       {slope:>8.4f} ms  ({slope * 1000:.1f} us)")
        print("    Projected overhead:")
        for nf in [100, 1000, 10000, 100000]:
            total = intercept + slope * nf
            print(f"      N={nf:>6} files:            {total:>8.1f} ms")
        print()


if __name__ == "__main__":
    main()
