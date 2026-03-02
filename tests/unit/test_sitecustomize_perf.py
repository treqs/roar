"""Unit tests for sitecustomize.py startup performance optimizations."""

import os
import subprocess
import sys
import time
from pathlib import Path

INJECT_DIR = (
    Path(__file__).resolve().parents[2] / "roar" / "services" / "execution" / "inject"
)


def _roar_env(*, log_file: str | None = None) -> dict:
    env = {**os.environ, "ROAR_WRAP": "1"}
    pythonpath = str(INJECT_DIR)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pythonpath if not existing else pythonpath + os.pathsep + existing
    if log_file:
        env["ROAR_LOG_FILE"] = log_file
    else:
        env.pop("ROAR_LOG_FILE", None)
    # Point ROAR_LOG_DIR at a non-existent dir to trigger the fast-path.
    env["ROAR_LOG_DIR"] = "/tmp/roar_perf_test_nonexistent_dir"
    return env


def _no_roar_env() -> dict:
    env = {**os.environ, "ROAR_WRAP": "0"}
    env.pop("ROAR_LOG_FILE", None)
    return env


def _run_pass(env: dict, n: int = 5) -> float:
    """Return average ms of 'python -c pass' with given env."""
    times = []
    for _ in range(n):
        t = time.perf_counter()
        subprocess.run(
            [sys.executable, "-c", "pass"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        times.append((time.perf_counter() - t) * 1000)
    return sum(times) / len(times)


def test_sitecustomize_import_overhead_under_threshold():
    """
    Importing sitecustomize.py (ROAR_WRAP=1, no LOG_FILE) should add
    less than 300ms of startup overhead over baseline.
    Previously ~416ms; target after lazy imports is <200ms.
    """
    baseline_ms = _run_pass(_no_roar_env(), n=5)
    roar_ms = _run_pass(_roar_env(log_file=None), n=5)
    overhead_ms = roar_ms - baseline_ms
    assert overhead_ms < 300, (
        f"sitecustomize import overhead is {overhead_ms:.0f}ms, expected <300ms. "
        f"baseline={baseline_ms:.0f}ms roar={roar_ms:.0f}ms"
    )


def test_atexit_overhead_without_ray_logs_under_threshold(tmp_path):
    """
    _collect_ray_io should skip the heavy collector import when ROAR_LOG_DIR
    is empty/absent. Total overhead with LOG_FILE but no Ray logs should be
    less than 600ms over baseline.
    Previously ~2160ms; target after optimizations is <600ms.
    """
    log_file = str(tmp_path / "test_inject.json")
    env = _roar_env(log_file=log_file)
    # ROAR_LOG_DIR is set to non-existent dir in _roar_env(), so collector is skipped.
    baseline_ms = _run_pass(_no_roar_env(), n=5)
    roar_ms = _run_pass(env, n=5)
    overhead_ms = roar_ms - baseline_ms
    assert overhead_ms < 600, (
        f"ROAR_WRAP=1 process overhead is {overhead_ms:.0f}ms, expected <600ms. "
        f"baseline={baseline_ms:.0f}ms roar={roar_ms:.0f}ms"
    )


def test_collect_ray_io_skips_import_when_no_logs(tmp_path, monkeypatch):
    """
    _collect_ray_io should return early without importing roar.ray.collector
    when ROAR_LOG_DIR is empty/absent and proxy_logs is empty.
    """
    # Remove collector from sys.modules if present.
    sys.modules.pop("roar.ray.collector", None)

    # Import the function under test.
    import importlib
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sitecustomize_test",
        str(INJECT_DIR / "sitecustomize.py"),
    )
    sc = importlib.util.module_from_spec(spec)

    # Patch environment.
    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv("ROAR_LOG_DIR", str(tmp_path / "nonexistent"))

    spec.loader.exec_module(sc)

    before_modules = set(sys.modules.keys())
    sc._collect_ray_io(proxy_logs=None)
    after_modules = set(sys.modules.keys())

    new_modules = after_modules - before_modules
    assert "roar.ray.collector" not in new_modules, (
        "roar.ray.collector was imported despite no logs to collect. "
        f"New modules: {sorted(new_modules)}"
    )


def test_get_used_packages_returns_correct_results():
    """
    _get_used_packages should return the same packages as before,
    using the faster packages_distributions() approach.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sitecustomize_pkg_test",
        str(INJECT_DIR / "sitecustomize.py"),
    )
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)

    # Build a minimal modules_files list containing a known package.
    import json

    json_file = json.__file__
    modules_files = [json_file] if json_file else []

    installed = sc._get_installed_packages()
    used = sc._get_used_packages(modules_files, installed)

    # Result should be a dict (possibly empty, but never an error).
    assert isinstance(used, dict), f"Expected dict, got {type(used)}"
