"""Tests for the ``from roar import require`` guard module."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_require(
    *, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROAR_")}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "from roar import require"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


@pytest.fixture
def require_module(monkeypatch):
    """Import ``roar.require`` with the guard temporarily bypassed."""
    monkeypatch.setenv("ROAR_GUARD", "0")
    return importlib.import_module("roar.require")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGuardEscapeHatch:
    def test_guard_passes_when_escape_hatch_set(self):
        result = _run_require(env_overrides={"ROAR_GUARD": "0"})
        assert result.returncode == 0

    def test_guard_passes_when_backend_job_marker_is_present(self):
        result = _run_require(env_overrides={"RAY_JOB_ID": "job-123"})
        assert result.returncode == 0

    def test_guard_exits_without_roar(self):
        result = _run_require()
        assert result.returncode == 1
        assert "roar run" in result.stderr

    def test_error_message_mentions_bypass(self):
        result = _run_require()
        assert "ROAR_GUARD=0" in result.stderr


class TestIsRoarProcess:
    def test_roar_cli(self, require_module):
        assert require_module._is_roar_process("roar") is True

    def test_truncated_preload(self, require_module):
        """Linux /proc comm truncates to 15 chars."""
        assert require_module._is_roar_process("roar-tracer-pre") is True

    def test_unrelated_process(self, require_module):
        assert require_module._is_roar_process("python3") is False
        assert require_module._is_roar_process("bash") is False
        assert require_module._is_roar_process("roar-something-else") is False


class TestModuleImportIdempotent:
    def test_second_import_does_not_retrigger(self):
        """Python caches modules; second import should be a no-op."""
        result = _run_require(env_overrides={"ROAR_GUARD": "0"})
        assert result.returncode == 0

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent("""\
                    from roar import require
                    from roar import require
                    print(\"ok\")
                """),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "ROAR_GUARD": "0"},
            timeout=10,
        )
        assert result.returncode == 0
        assert "ok" in result.stdout
