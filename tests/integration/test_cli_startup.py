"""
Integration test for CLI startup performance.

Verifies that the CLI starts quickly, especially for --help.
"""

import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.integration

# Target: help should render in under 200ms
# Current baseline is ~400ms, we want to cut it in half
HELP_TIME_TARGET_MS = 200


def test_cli_help_startup_time():
    """CLI --help should render quickly (< 200ms)."""
    # Warm up (first run may be slower due to .pyc compilation)
    subprocess.run(
        [sys.executable, "-m", "roar", "--help"],
        capture_output=True,
        check=True,
    )
    
    # Measure actual time (average of 3 runs)
    times = []
    for _ in range(3):
        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "roar", "--help"],
            capture_output=True,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)
        assert result.returncode == 0
    
    avg_ms = sum(times) / len(times)
    
    # This test will fail until we implement lazy loading
    assert avg_ms < HELP_TIME_TARGET_MS, (
        f"CLI --help took {avg_ms:.0f}ms average, target is <{HELP_TIME_TARGET_MS}ms. "
        f"Individual runs: {[f'{t:.0f}ms' for t in times]}"
    )


def test_cli_version_startup_time():
    """CLI --version should be even faster than --help."""
    # Warm up
    subprocess.run(
        [sys.executable, "-m", "roar", "--version"],
        capture_output=True,
        check=True,
    )
    
    # Measure
    times = []
    for _ in range(3):
        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "roar", "--version"],
            capture_output=True,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        times.append(elapsed_ms)
        assert result.returncode == 0
    
    avg_ms = sum(times) / len(times)
    
    # Version should be fast - even less than help target
    assert avg_ms < HELP_TIME_TARGET_MS, (
        f"CLI --version took {avg_ms:.0f}ms average, target is <{HELP_TIME_TARGET_MS}ms"
    )
